# SEO Health Checker

A small service that fetches a page safely, analyzes its HTML for SEO health,
and returns a 0–100 score with actionable issues (messages in Persian).

## Project layout

```
app/
  __init__.py     # package exports
  contracts.py    # shared dataclasses & enums (no framework coupling)
  schemas.py      # Pydantic request/response models (HTTP contract)
fetcher/         # TODO: safe page fetching
analyzer/        # TODO: HTML analysis & measurements
scorer/          # TODO: issue generation & scoring
```

## HTTP contract

**Request** (`POST /health-check`):

```json
{ "url": "https://example.com/" }
```

**Response**:

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

## Shared types

| Type | Location | Purpose |
| --- | --- | --- |
| `Severity` | `app.contracts` | `critical` / `warning` / `info` enum |
| `Issue` | `app.contracts` | `code`, `severity`, `message` (Persian), `details` |
| `FetchResult` | `app.contracts` | fetch outcome: URL, status, headers, body, elapsed time |
| `AnalysisResult` | `app.contracts` | measurements, internal links, discovered issues |
| `HealthReport` | `app.contracts` | aggregated report consumed by the response schema |
| `HealthCheckRequest` / `HealthCheckResponse` / `IssueResponse` | `app.schemas` | Pydantic HTTP models |

See [DEVELOPMENT.md](DEVELOPMENT.md) for the module interfaces other agents
must implement.