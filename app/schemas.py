"""Pydantic request/response models for the SEO Health Checker API.

These models are the public contract of the HTTP layer. Internal modules should
prefer the plain dataclasses in :mod:`app.contracts`; this file exists to
translate between JSON and those contracts.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator


class HealthCheckRequest(BaseModel):
    """Incoming request body.

    Example::

        {"url": "https://example.com/"}
    """

    url: str = Field(..., description="URL of the page to health-check.")

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")
        return value


class IssueResponse(BaseModel):
    """A single issue in the serialized response.

    Mirrors :class:`app.contracts.Issue` but uses plain strings for JSON.
    """

    code: str = Field(..., description="Stable machine-readable issue code.")
    severity: str = Field(
        ..., description="One of: critical, warning, info."
    )
    message: str = Field(..., description="Human-readable message in Persian.")
    details: Optional[str] = Field(
        None, description="Optional supporting context."
    )

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"critical", "warning", "info"}:
            raise ValueError(
                "severity must be one of: critical, warning, info"
            )
        return value


class HealthCheckResponse(BaseModel):
    """Serialized health-check result."""

    requested_url: str = Field(..., description="URL as provided in the request.")
    final_url: str = Field(
        ..., description="Final URL after redirects."
    )
    status: int = Field(..., description="HTTP status code of the response.")
    response_time_ms: float = Field(
        ..., description="Total response time in milliseconds."
    )
    score: int = Field(
        ..., description="Aggregate SEO score from 0 to 100."
    )
    measurements: Dict[str, Union[float, int, bool, None]] = Field(
        default_factory=dict,
        description="Named measurements produced by the analyzer.",
    )
    issues: List[IssueResponse] = Field(
        default_factory=list,
        description="Discovered SEO issues, ordered by severity.",
    )

    @field_validator("score")
    @classmethod
    def _validate_score(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("score must be between 0 and 100")
        return value