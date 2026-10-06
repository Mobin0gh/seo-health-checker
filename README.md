# SEO Health Checker

A small service that fetches a page safely (SSRF-hardened), analyzes its HTML
for SEO health, and returns a 0–100 score with actionable issues (messages in
Persian).

## Quick start

```bash
# Install runtime dependencies
pip install -r requirements.txt

# Run the API
uvicorn app.api:app --host 0.0.0.0 --port 8000
```

## Project layout

```
app/
  __init__.py     # package exports
  contracts.py    # shared dataclasses & enums (no framework coupling)
  schemas.py      # Pydantic request/response models (HTTP contract)
  security.py     # SSRF-hardened async HTTP client (Task 2)
  analyzer/       # HTML analysis & measurements (Task 3)
  linkchecker/    # robots.txt, sitemap.xml, internal links (Task 4)
  scorer/         # issue generation & scoring (Task 5)
  orchestrator.py # ties modules together (Task 6)
  api.py          # FastAPI app + rate limiting (Task 6)
  ratelimit.py    # per-client-IP rolling limiter (Task 6)
```

## HTTP contract

**Request** (`POST /check`):

```json
{ "url": "https://example.com/" }
```

**Response** (`HealthCheckResponse`):

```json
{
  "requested_url": "https://example.com/",
  "final_url": "https://example.com/",
  "status": 200,
  "response_time_ms": 123.4,
  "score": 78,
  "measurements": { "title_length": 52, "has_meta_description": true },
  "issues": [
    {
      "code": "missing_meta_description",
      "severity": "warning",
      "message": "صفحه دارای متا توصیف نیست.",
      "details": null
    }
  ]
}
```

### Error mapping

| Condition | HTTP | Body `detail` |
|-----------|------|---------------|
| Invalid request / malformed URL | 400 | "Invalid request" |
| SSRF / security-policy refusal | 403 | "Request refused" |
| Outer timeout (backstop) | 504 | "Check timed out" |
| DNS / connect / redirect / oversized | 502 | "Upstream unavailable" |
| Unexpected programming error | 500 | "Internal error" |

A fetched non-2xx (e.g. 404) is a *successful check*: returns HTTP 200 with
the `http_error` issue and the scorer's report.

## Rate limiting

`POST /check` allows **5 requests per rolling 60-second window per client IP**.
The actual ASGI peer address (`request.client.host`) is used; `X-Forwarded-For`
and `X-Real-IP` are never trusted. On exceed, HTTP 429 is returned with a
correct `Retry-After` header. The limiter is in-memory and per-process (no
Redis).

## Scoring

The deterministic scorer (`HealthScorer`) applies ceiling penalties across:
fetch failure / HTTP error / slow response, robots.txt, sitemap, broken links,
and content (analyzer issues). Final score is clamped to 0–100.

## Shared types

| Type | Location | Purpose |
| --- | --- | --- |
| `Severity` | `app.contracts` | `critical` / `warning` / `info` enum |
| `Issue` | `app.contracts` | `code`, `severity`, `message` (Persian), `details` |
| `FetchResult` | `app.contracts` | fetch outcome: URL, status, headers, body, elapsed time |
| `AnalysisResult` | `app.contracts` | measurements, internal links, discovered issues |
| `HealthReport` | `app.contracts` | aggregated report consumed by the response schema |
| `HealthCheckRequest` / `HealthCheckResponse` / `IssueResponse` | `app.schemas` | Pydantic HTTP models |

## Docker

```bash
docker build -t seo-health-checker .
docker run -p 8000:8000 seo-health-checker
```

## Tests

```bash
python -X utf8 -m unittest discover -s tests
```

## Security limitations

* In-memory rate limiter is per-process; it does not survive restarts and
  is not shared across workers.
* `app/security.py` validates DNS once (before connect) and rewrites the
  outbound URL to the validated IP literal, preserving the original hostname
  for Host / SNI.
* Environment proxies are disabled (`trust_env=False`).
* Response body is streamed with a hard 2 MiB cap.
* No external secrets, API keys, or proxy configuration are accepted.