"""SEO Health Checker — shared package.

This package exposes the stable contracts that other modules/agents build on:

* :mod:`app.contracts`  — plain dataclasses and enums shared between modules.
* :mod:`app.schemas`   — Pydantic request/response models for the HTTP API.

Nothing here implements fetching, analysis, or scoring. Those live in their own
modules and only depend on the types defined below.
"""

from app.contracts import (
    AnalysisResult,
    FetchResult,
    HealthReport,
    Issue,
    Severity,
)
from app.schemas import (
    HealthCheckRequest,
    HealthCheckResponse,
    IssueResponse,
)

__all__ = [
    "AnalysisResult",
    "FetchResult",
    "HealthReport",
    "HealthCheckRequest",
    "HealthCheckResponse",
    "Issue",
    "IssueResponse",
    "Severity",
]

__version__ = "0.1.0"