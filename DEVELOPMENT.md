# Development note — module interfaces

This document defines the interfaces other agents must implement. The shared
schemas in `app/schemas.py` and the contracts in `app/contracts.py` **must not
be modified** when implementing these modules — the interfaces are designed to
fit the existing contracts exactly.

---

## 1. Page fetcher (`fetcher/`)

**Interface:** `fetcher/fetcher.py`

```python
from app.contracts import FetchResult

class PageFetcher:
    def __init__(
        self,
        timeout_ms: int = 10000,
        max_redirects: int = 5,
        user_agent: str = "SEOHealthChecker/0.1",
        allowed_schemes: tuple[str, ...] = ("http", "https"),
    ) -> None: ...

    async def fetch(self, url: str) -> FetchResult:
        """Fetch a page safely.

        Safety requirements:
        * Reject non-HTTP(S) schemes and malformed URLs.
        * Enforce a connection/read timeout.
        * Limit redirect count; reject further redirects.
        * Cap response body size (e.g. 2 MB) and abort oversized responses.
        * Never follow redirects to private/loopback/link-local addresses
          (SSRF protection).
        * Decode the body as UTF-8 (fall back to detected encoding on failure).

        Returns a populated ``FetchResult``. On unrecoverable failure raise an
        exception — the orchestrator decides whether to surface it or to
        return a zero-score report with an appropriate issue.
        """
        ...
```

**Consumes:** raw URL string.  
**Produces:** `FetchResult` with `url`, `final_url`, `status`, `headers`,
`body`, `elapsed_ms`, `content_type`, `redirect_count`.

---

## 2. HTML analyzer (`analyzer/`)

**Interface:** `analyzer/analyzer.py`

```python
from app.contracts import AnalysisResult, FetchResult

class HtmlAnalyzer:
    def __init__(self, base_url: str) -> None: ...

    def analyze(self, html: str) -> AnalysisResult:
        """Parse ``html`` and produce measurements + discovered links.

        Implementations must be side-effect free and safe against malformed or
        non-HTML input.

        Returns ``AnalysisResult`` with:
        * ``measurements`` — a flat dict of named values
          (`float | int | bool | None`). Suggested keys (additive, not
          exhaustive): ``title``, ``title_length``, ``meta_description``,
          ``meta_description_length``, ``h1_count``, ``h1_texts``,
          ``canonical``, ``robots_meta``, ``og_title``, ``image_count``,
          ``image_alt_missing``, ``word_count``, ``has_ssl``.
        * ``internal_links`` — absolute URLs of discovered internal links.
        * ``issues`` — issues discovered during analysis (may be empty here;
          scoring may add more).
        """
        ...
```

**Consumes:** `FetchResult` (or its `body`/`final_url`).  
**Produces:** `AnalysisResult`.

---

## 3. Scorer (`scorer/`)

**Interface:** `scorer/scorer.py`

```python
from app.contracts import AnalysisResult, FetchResult, HealthReport, Issue, Severity

class HealthScorer:
    def score(
        self,
        fetch: FetchResult,
        analysis: AnalysisResult,
    ) -> HealthReport:
        """Generate issues and compute a 0–100 score.

        Rules:
        * Critical issues (e.g. non-2xx status, SSL failure) drive the score
          toward 0.
        * Warnings (e.g. missing meta description, long title) lower the score
          moderately.
        * Info items are advisory and do not materially affect the score.
        * The final score is clamped to ``[0, 100]``.
        * Issues must each carry a stable ``code``, a ``Severity``, a Persian
          ``message``, and optional ``details``.

        Returns a populated ``HealthReport``. ``measurements`` must be the
        analyzer's measurements merged with any fetch-level measurements.
        """
        ...
```

**Consumes:** `FetchResult`, `AnalysisResult`.  
**Produces:** `HealthReport`.

---

## Wiring note

The orchestrator (not part of this scope) will call them in order:

```
fetcher.fetch(url) → analyzer.analyze(body) → scorer.score(fetch, analysis)
```

and serialize the resulting `HealthReport` via `app.schemas.HealthCheckResponse`.
Do not introduce new cross-module dependencies — each module should only import
from `app.contracts` / `app.schemas`.