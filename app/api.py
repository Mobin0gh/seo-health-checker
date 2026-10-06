"""FastAPI application for the SEO Health Checker.

Provides ``POST /check`` which runs the full orchestration pipeline
(fetch → analyze → resource checks → score) and returns the
existing :class:`app.schemas.HealthCheckResponse`.

Rate limiting: per-client-IP rolling window, 5 requests / 60 seconds.
The limiter is an in-memory singleton attached to ``app.state``.
"""

from __future__ import annotations

import math
import logging

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.orchestrator import (
    CheckTimeout,
    InvalidRequestError,
    OrchestratorError,
    SecurityPolicyRefused,
    UnexpectedError,
    UpstreamUnavailable,
    run_check,
)
from app.ratelimit import RateLimitExceeded, RollingRateLimiter
from app.schemas import HealthCheckRequest, HealthCheckResponse

logger = logging.getLogger(__name__)

app = FastAPI(title="SEO Health Checker", version="0.1.0")
app.state.rate_limiter = RollingRateLimiter()


@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/check":
        ip = request.client.host if request.client else "unknown"
        try:
            await request.app.state.rate_limiter.check(ip)
        except RateLimitExceeded as exc:
            retry_after = max(1, math.ceil(exc.retry_after_ms / 1000))
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"detail": "Invalid request"},
    )


@app.post("/check", response_model=HealthCheckResponse)
async def check(body: HealthCheckRequest):
    url = body.url.strip()
    try:
        report = await run_check(url)
    except InvalidRequestError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request",
        )
    except SecurityPolicyRefused:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request refused",
        )
    except UpstreamUnavailable:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upstream unavailable",
        )
    except CheckTimeout:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Check timed out",
        )
    except UnexpectedError:
        logger.exception("Unexpected orchestrator error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal error",
        )
    except OrchestratorError:
        raise

    return HealthCheckResponse(
        requested_url=report.requested_url,
        final_url=report.final_url,
        status=report.status,
        response_time_ms=report.response_time_ms,
        score=report.score,
        measurements=report.measurements,
        issues=[
            {
                "code": i.code,
                "severity": i.severity.value,
                "message": i.message,
                "details": i.details,
            }
            for i in report.issues
        ],
    )