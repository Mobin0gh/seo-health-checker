"""Deterministic SEO health scorer package.

This package contains a pure, synchronous scorer that converts structured
fetch, robots, sitemap, and link-check outcomes into a
:class:`app.contracts.HealthReport` with a 0-100 score and a deterministic,
ordered list of issues. It performs no network or asynchronous work.
"""

from .scorer import HealthScorer, ResourceState, ScoreInput

__all__ = ["HealthScorer", "ResourceState", "ScoreInput"]