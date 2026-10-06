# Development note — module interfaces

This document defines the interfaces other agents must implement. The shared
schemas in `app/schemas.py` and the contracts in `app/contracts.py` **must not
be modified** when implementing these modules — the interfaces are designed to
fit the existing contracts exactly.

---

## 1. Page fetcher (`app/security.py`)

**Interface:** `app/security.py` exposes the async entry point:

```python
from app.security import fetch_url, SecurityError, URLValidationError

async def fetch_url(
    url: str,
    *,
    deadline_ms: float,
    user_agent: str = "SEOHealthChecker/0.1",
    max_redirects: int = 5,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FetchResult: ...
```

**Consumes:** raw URL string + an absolute monotonic deadline (ms).  
**Produces:** `FetchResult` with `url`, `final_url`, `status`, `headers`,
`body`, `elapsed_ms`, `content_type`, `redirect_count`.

Exceptions raised: `URLValidationError`, `DNSResolutionError`,
`ResponseTooLarge`, `FetchTimeout`, `RedirectLimitExceeded`, `SecurityError`
(all subclasses of `SecurityError`).

The orchestrator (`app/orchestrator.py`) calls `app.security.fetch_url`
through the module reference so tests can patch it.

---

## 2. HTML analyzer (`app/analyzer/`)

**Interface:** `app/analyzer/analyzer.py`

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

## 4. Scorer (`app/scorer/`)

**Interface:** `app/scorer/scorer.py`

```python
from app.contracts import AnalysisResult, FetchResult, HealthReport, Issue, Severity
from app.scorer import ResourceState, ScoreInput

class HealthScorer:
    def score(self, inputs: ScoreInput) -> HealthReport:
        """Generate issues and compute a 0–100 score.

        Rules:
        * Critical issues (e.g. fetch failure, non-2xx status) drive the score
          toward 0.
        * Warnings (e.g. slow response, missing sitemap) lower the score
          moderately.
        * Info items are advisory and do not materially affect the score.
        * The final score is clamped to ``[0, 100]``.
        * Issues must each carry a stable ``code``, a ``Severity``, a Persian
          ``message``, and optional ``details``.

        Returns a populated ``HealthReport``. ``measurements`` includes both
        analyzer measurements and scorer-specific measurements with stable,
        namespaced keys (e.g., ``scorer.http_status``, ``scorer.robots_exists``).
        """
        ...
```

**Consumes:** `ScoreInput` (see below).
**Produces:** `HealthReport`.

---

**Produces:** `HealthReport`. ``measurements`` includes both
analyzer measurements and scorer-specific measurements with stable,
namespaced keys (e.g., ``scorer.http_status``,
``scorer.robots_exists``).
```

**Consumes:** `ScoreInput` (see below).
**Produces:** `HealthReport`.

---

## 3. Resource & link checker (`app/linkchecker/`)

**Interface:** `app/linkchecker/__init__.py` re-exports:
- `ResourceChecker` with `check_robots`, `check_sitemap`, `check_all`
- `HtmlAnalyzer` is *not* here; it lives in `app/analyzer/`

### ResourceChecker
```python
from app.linkchecker import ResourceChecker
from app.linkchecker.results import (
    ResourceExists,
    ResourceCheckResult,
    SitemapCheckResult,
    LinkCheckSummary,
)

class ResourceChecker:
    @staticmethod
    async def check_robots(
        base_url: str,
        deadline_ms: float,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> ResourceCheckResult: ...

    @staticmethod
    async def check_sitemap(
        base_url: str,
        deadline_ms: float,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> SitemapCheckResult: ...

    @staticmethod
    async def check_all(
        base_url: str,
        links: List[str],
        deadline_ms: float,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> tuple[ResourceCheckResult, SitemapCheckResult, LinkCheckSummary]: ...
```

**Consumes for robots/sitemap:** final page URL (after redirects) whose origin defines
the resource location, plus an absolute monotonic deadline (ms).  
**Produces:** populated result objects with existence (three-state), HTTP status,
reason, detail, body (for robots/sitemap), elapsed_ms, and for sitemap:
validity flag and URL count.

**Consumes for link checking:** list of absolute URLs discovered on the page,
same deadline.  
**Produces:** `LinkCheckSummary` containing per-link results in input order
(after dedup + 20-cap), checked count, broken count.

All network calls go through the same `app.security.fetch_url` with the
shared deadline. The checker respects:
- Bounded concurrency (max 5) for link checks.
- At most first 20 unique links (order preserved).
- Three-state existence (`ResourceExists.TRUE/FALSE/UNKNOWN`).
- Known exceptions mapped to explicit reason strings.
- XML parsing that rejects DTDs (XXE safety).

**Consumes for the orchestrator:** the orchestrator calls `check_all`
with the deduplicated, capped link list and the same absolute deadline
used for the main fetch.

---

### ScoreInput

A single dataclass carrying all inputs needed to produce a health report.

```python
from dataclasses import dataclass, field
from typing import Optional
from app.contracts import AnalysisResult, FetchResult
from app.scorer import ResourceState

@dataclass
class ScoreInput:
    requested_url: str
    # Fetch
    fetch: Optional[FetchResult] = None
    fetch_failure_code: Optional[str] = None  # sanitized when fetch failed
    # Analysis
    analysis: Optional[AnalysisResult] = None
    analysis_failed: bool = False
    analysis_reason: Optional[str] = None
    # Robots
    robots_exists: Optional[ResourceState] = None
    robots_status: Optional[int] = None
    robots_reason: Optional[str] = None
    # Sitemap
    sitemap_exists: Optional[ResourceState] = None
    sitemap_valid: Optional[bool] = None
    sitemap_status: Optional[int] = None
    sitemap_url_count: int = 0
    sitemap_reason: Optional[str] = None
    # Links
    total_unique_links: int = 0
    checked_links: int = 0
    confirmed_broken_count: int = 0
    broken_url_samples: list[str] = field(default_factory=list)
    links_deadline_interrupted: bool = False
```

---

## Wiring note

The orchestrator (`app/orchestrator.py`) calls them in order:

```
app.security.fetch_url(url, deadline_ms) → HtmlAnalyzer(base_url).analyze(body)
    → ResourceChecker.check_all(base_url, links, deadline_ms)
    → HealthScorer.score(ScoreInput(...)) → HealthReport
```

and `app/api.py` serializes the resulting `HealthReport` via
`app.schemas.HealthCheckResponse`.
The orchestrator does not import Task 4's private/local result types; it
translates them into `ScoreInput` using only public attributes.