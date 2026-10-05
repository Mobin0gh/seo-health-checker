# Project Context

## 1. Project Purpose

Secure SEO + Technical Website Health Checker API using FastAPI. Fetches a page safely with SSRF-hardened HTTP client, analyzes HTML for SEO health, and returns a 0-100 score with actionable issues (messages in Persian).

## 2. Intended API

POST /check
Request: {"url":"https://example.com"}
Response: score 0-100, measurements, issues (code/severity/message in Persian), requested URL, final URL, status, response time.

## 3. Intended Functional Requirements

HTTP status, response time, HTTPS, HTTP->HTTPS redirect, title length, meta description length, H1 count, meta viewport, images missing/empty alt, robots.txt, sitemap.xml, canonical, HTML size, first 20 unique internal links, broken internal links.

## 4. Security Requirements

Non-negotiable SSRF/network/security requirements:
- only HTTP/HTTPS, only ports 80/443
- reject URL credentials
- resolve hostname before connecting
- reject private/loopback/link-local/reserved/multicast/unspecified/non-global IPs
- reject mixed public + forbidden DNS answers
- protect against DNS rebinding (validate once, connect to validated IP)
- actual TCP connection must use validated IP
- preserve original hostname for HTTP Host and HTTPS SNI/certificate verification
- HTTP client must not independently resolve hostname again
- redirects followed manually, every redirect target validated again
- small redirect limit
- one absolute monotonic deadline for entire operation
- DNS, connection, redirects, body reading respect remaining deadline
- stream response body, max 2 MiB
- disable environment-configured proxies
- POST /check max 5 requests per rolling minute per client IP
- do not blindly trust arbitrary forwarded headers

## 5. Current Architecture

| Path | Responsibility | Status |
|------|---------------|--------|
| app/contracts.py | Shared dataclasses/enums (Severity, Issue, FetchResult, AnalysisResult, HealthReport) | IMPLEMENTED |
| app/schemas.py | Pydantic request/response models (HealthCheckRequest, HealthCheckResponse, IssueResponse) | IMPLEMENTED |
| app/__init__.py | Package exports + __version__ | IMPLEMENTED |
| app/security.py | SSRF-hardened async HTTP client (validate_url, resolve_and_validate_host, rewrite_url_to_ip, _ValidatedTransport, fetch_url) | IMPLEMENTED |
| tests/test_security.py | 66 unittest tests covering URL validation, DNS resolution, public routability, URL rewriting, successful fetches, failure cases | IMPLEMENTED |
| fetcher/ | TODO per README — not implemented | PLANNED |
| analyzer/ | TODO per README — not implemented | PLANNED |
| scorer/ | TODO per README — not implemented | PLANNED |
| main.py / FastAPI app | Not present | NOT IMPLEMENTED |

## 6. Implementation Status

| Task | Status | Evidence |
|------|--------|----------|
| Task 1 | IMPLEMENTED AND VERIFIED | app/contracts.py, app/schemas.py, app/__init__.py exist; imports work; shared types match DEVELOPMENT.md interface |
| Task 2 | IMPLEMENTED AND VERIFIED | app/security.py implements SSRF-hardened fetcher; 66/66 tests pass; all security requirements implemented and tested |
| Task 3 | IMPLEMENTED AND VERIFIED | app/analyzer/__init__.py, app/analyzer/analyzer.py; 62/62 tests pass; all 128 tests pass (66 security + 62 analyzer) |
| Task 4 | Not started | No robots/sitemap/internal-links module |
| Task 5 | Not started | No scoring/issue generation module |
| Task 6 | Not started | No integration/rate-limiting/Docker |

## 7. Specification Gaps / Discrepancies

1. README says POST /health-check but intended spec says POST /check — endpoint path mismatch
2. README lists fetcher/analyzer/scorer as directories but actual implementation puts fetcher in app/security.py (flat module) — architecture differs from README layout
3. DEVELOPMENT.md specifies PageFetcher.fetch() async interface; security.py implements fetch_url() async function — different pattern, but functionally equivalent
4. POST /check rate limiting (5 req/min per client IP) is specified but not implemented — no FastAPI app exists yet
5. Forwarded header handling is specified but not applicable until API layer exists
6. "broken internal links" check requires HTTP client + link extraction — depends on Tasks 2 and 4

## 8. Roadmap

- Task 1 — contracts/project skeleton ✅ COMPLETE
- Task 2 — secure HTTP fetcher + security tests ✅ COMPLETE
- Task 3 — HTML analyzer ⏳ PLANNED
- Task 4 — robots/sitemap + internal links ⏳ PLANNED
- Task 5 — scoring + issues ⏳ PLANNED
- Task 6 — integration + rate limiting + Docker + full test suite ⏳ PLANNED

## 9. Agent Workflow

- Planner / Plan = analysis and planning
- Build = implementation and tests
- Explore = read-only investigation
- Reviewer = independent review/security/regression/test review

Future agents MUST read PROJECT.md before planning or editing. If PROJECT.md conflicts with repository evidence, repository evidence takes precedence and the conflict must be reported.

## 10. Skill Strategy

Skills should be selected according to the task:
- context-engineering, planning-and-task-breakdown, spec-driven-development (planning)
- incremental-implementation, test-driven-development, source-driven-development (building)
- code-review-and-quality, security-and-hardening (review)
- debugging-and-error-recovery (fixing)
- documentation-and-adrs (documentation)

Do not claim that every agent automatically loads every skill.

## 11. Definition of Done

A task is complete only when:
- implementation satisfies its requirements
- relevant tests exist
- focused tests pass
- full test suite passes before task completion
- security requirements are reviewed where applicable
- API contracts remain compatible
- documentation is updated where necessary

## 12. Known Limitations

- Only Task 1 and Task 2 are implemented; Tasks 3-6 are planned
- No FastAPI app exists yet, so no runtime API to test
- No git repository initialized (git status: "not a git repository")
- README layout (fetcher/analyzer/scorer dirs) does not match actual flat app/ module layout
- Rate limiting specification not yet implementable (no API layer)
