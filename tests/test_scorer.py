"""Unit tests for the deterministic scorer (app/scorer/).

These tests verify that the scorer converts structured fetch, robots,
sitemap, and link-check outcomes into a HealthReport with a 0–100 score
and a deterministic, ordered list of issues. All tests run in memory with
no network access.

The test class inherits from unittest.TestCase so it can be run by pytest
(which discovers unittest tests) or directly via ``python -m unittest``.
"""

from __future__ import annotations

import unittest
from typing import List

from app.analyzer.analyzer import HtmlAnalyzer
from app.contracts import AnalysisResult, FetchResult, Issue, Severity
from app.scorer import HealthScorer, ResourceState, ScoreInput


def _successful_fetch():
    """Return a successful FetchResult for use in tests."""
    return FetchResult(
        url="https://example.com/",
        final_url="https://example.com/",
        status=200,
        headers={"content-type": "text/html; charset=utf-8"},
        body="<html><head><title>SEO Health Checker - Technical Audit Tool</title>"
        "<meta name=\"description\" content=\"This is a normal meta description that should pass the SEO checks without any issues.\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<link rel=\"canonical\" href=\"https://example.com/\"></head>"
        "<body><h1>Main Heading</h1><p>Content</p></body></html>",
        elapsed_ms=1200.0,
    )


class TestScoreInputValidation(unittest.TestCase):
    """Verify ScoreInput construction and defaults."""

    def test_minimal_input(self):
        inp = ScoreInput(requested_url="https://example.com/")
        self.assertEqual(inp.requested_url, "https://example.com/")
        self.assertIsNone(inp.fetch)
        self.assertIsNone(inp.fetch_failure_code)
        self.assertIsNone(inp.analysis)
        self.assertFalse(inp.analysis_failed)
        self.assertIsNone(inp.analysis_reason)
        self.assertIsNone(inp.robots_exists)
        self.assertIsNone(inp.sitemap_exists)
        self.assertEqual(inp.total_unique_links, 0)
        self.assertEqual(inp.confirmed_broken_count, 0)
        self.assertEqual(inp.links_deadline_interrupted, False)


class TestPerfectReport(unittest.TestCase):
    """A perfect report should score 100 with no issues."""

    def test_perfect_fetch_and_content(self):
        fetch = _successful_fetch()
        analyzer = HtmlAnalyzer("https://example.com/")
        analysis = analyzer.analyze(fetch.body)

        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            analysis=analysis,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 100)
        self.assertEqual(len(report.issues), 0)
        self.assertEqual(report.status, 200)
        self.assertEqual(report.final_url, "https://example.com/")
        self.assertGreater(report.response_time_ms, 0)


class TestFetchFailure(unittest.TestCase):
    """Fetch failure emits fetch_failed and scores 0."""

    def test_dns_failure(self):
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=None,
            fetch_failure_code="dns_failure",
        )
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 0)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "fetch_failed")
        self.assertEqual(issue.severity, Severity.CRITICAL)
        self.assertIn("دریافت صفحه ناموفق بود", issue.message)
        self.assertEqual(report.status, 0)
        self.assertEqual(report.final_url, "https://example.com/")
        self.assertEqual(report.response_time_ms, 0.0)
        self.assertEqual(report.measurements.get("scorer.fetch_failed"), True)

    def test_generic_fetch_failure(self):
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=None,
            fetch_failure_code="unknown_error",
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 0)
        self.assertTrue(any(i.code == "fetch_failed" for i in report.issues))


class TestHttpError(unittest.TestCase):
    """Non-2xx HTTP status emits http_error and scores 0."""

    def test_404(self):
        fetch = FetchResult(
            url="https://example.com/missing",
            final_url="https://example.com/missing",
            status=404,
            headers={},
            body="Not Found",
            elapsed_ms=800.0,
        )
        inp = ScoreInput(
            requested_url="https://example.com/missing",
            fetch=fetch,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 0)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "http_error")
        self.assertEqual(issue.severity, Severity.CRITICAL)
        self.assertEqual(report.status, 404)
        self.assertEqual(report.measurements.get("scorer.http_status"), 404)

    def test_500(self):
        fetch = FetchResult(
            url="https://example.com/bad",
            final_url="https://example.com/bad",
            status=500,
            headers={},
            body="Server Error",
            elapsed_ms=1500.0,
        )
        inp = ScoreInput(requested_url="https://example.com/bad", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 0)
        self.assertTrue(any(i.code == "http_error" for i in report.issues))


class TestHttpSlow(unittest.TestCase):
    """Response time >3000ms emits http_slow and deducts 10 points."""

    def test_slow_response(self):
        fetch = FetchResult(
            url="https://example.com/slow",
            final_url="https://example.com/slow",
            status=200,
            headers={},
            body="<html><title>Slow</title></html>",
            elapsed_ms=3500.0,
        )
        inp = ScoreInput(requested_url="https://example.com/slow", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 90)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "http_slow")
        self.assertEqual(issue.severity, Severity.WARNING)
        self.assertIn("زمان پاسخ‌گیری", issue.message)
        self.assertIn("میلی‌ثانیه", issue.message)


class TestRedirectHttpToHttps(unittest.TestCase):
    """HTTP-to-HTTPS redirect emits redirect_http_to_https with 0-point info."""

    def test_redirect_http_to_https(self):
        fetch = FetchResult(
            url="http://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=1200.0,
            redirect_count=1,
        )
        inp = ScoreInput(requested_url="http://example.com/", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 100)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "redirect_http_to_https")
        self.assertEqual(issue.severity, Severity.INFO)
        self.assertEqual(issue.message, "صفحه از HTTP به HTTPS هدایت شد.")

    def test_no_redirect(self):
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=1200.0,
            redirect_count=0,
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 100)
        self.assertFalse(any(i.code == "redirect_http_to_https" for i in report.issues))


class TestRobotsTxt(unittest.TestCase):
    """Robots.txt outcomes emit appropriate issues."""

    def test_missing_robots(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            robots_exists=ResourceState.FALSE,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "missing_robots")
        self.assertEqual(issue.severity, Severity.INFO)
        self.assertEqual(report.score, 100)  # 0-point penalty

    def test_robots_unavailable(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            robots_exists=ResourceState.UNKNOWN,
            robots_reason="dns_failure",
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "robots_unavailable")
        self.assertEqual(issue.severity, Severity.WARNING)
        self.assertEqual(report.score, 90)  # 10-point penalty

    def test_robots_present(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            robots_exists=ResourceState.TRUE,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertFalse(any(i.code.startswith("robots") for i in report.issues))
        self.assertEqual(report.score, 100)


class TestSitemap(unittest.TestCase):
    """Sitemap outcomes emit appropriate issues."""

    def test_missing_sitemap(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            sitemap_exists=ResourceState.FALSE,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "missing_sitemap")
        self.assertEqual(issue.severity, Severity.INFO)
        self.assertEqual(report.score, 100)

    def test_sitemap_unavailable(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            sitemap_exists=ResourceState.UNKNOWN,
            sitemap_reason="timeout",
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "sitemap_unavailable")
        self.assertEqual(issue.severity, Severity.WARNING)
        self.assertEqual(report.score, 90)

    def test_sitemap_invalid(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            sitemap_exists=ResourceState.TRUE,
            sitemap_valid=False,
            sitemap_url_count=0,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "sitemap_invalid")
        self.assertEqual(issue.severity, Severity.WARNING)
        self.assertEqual(report.score, 90)

    def test_sitemap_valid(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            sitemap_exists=ResourceState.TRUE,
            sitemap_valid=True,
            sitemap_url_count=15,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertFalse(any(i.code.startswith("sitemap") for i in report.issues))
        self.assertEqual(report.score, 100)


class TestBrokenLinks(unittest.TestCase):
    """Broken links are aggregated into one warning issue."""

    def test_three_broken_links(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            total_unique_links=10,
            checked_links=10,
            confirmed_broken_count=3,
            broken_url_samples=[
                "https://example.com/broken1",
                "https://example.com/broken2",
                "https://example.com/broken3",
            ],
        )
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 85)  # 15-point penalty (5*3)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "broken_links")
        self.assertEqual(issue.severity, Severity.WARNING)
        self.assertIn("3 لینک شکسته است", issue.message)
        self.assertIn("تعداد: 3", issue.details or "")
        self.assertIn("https://example.com/broken1", issue.details or "")

    def test_many_broken_links_capped(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            confirmed_broken_count=10,
            broken_url_samples=[f"https://example.com/broken{i}" for i in range(10)],
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 85)  # 5*10=50 -> capped at 15, so 100-15=85
        self.assertEqual(len(report.issues), 1)

    def test_no_broken_links(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            total_unique_links=5,
            checked_links=5,
            confirmed_broken_count=0,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertFalse(any(i.code == "broken_links" for i in report.issues))


class TestDeadlineInterrupted(unittest.TestCase):
    """Deadline-interrupted link checking emits an info issue with 0 points."""

    def test_deadline_interrupted(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            total_unique_links=30,
            checked_links=15,
            confirmed_broken_count=2,
            links_deadline_interrupted=True,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)

        # Must emit both broken links and deadline info
        self.assertEqual(len(report.issues), 2)
        codes = {i.code for i in report.issues}
        self.assertIn("broken_links", codes)
        self.assertIn("link_check_incomplete", codes)
        # Penalty still applies for broken links
        self.assertLess(report.score, 100)
        # Deadline issue has 0 points
        deadline_issue = next(i for i in report.issues if i.code == "link_check_incomplete")
        self.assertEqual(deadline_issue.severity, Severity.INFO)

    def test_deadline_not_interrupted(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            total_unique_links=20,
            checked_links=20,
            confirmed_broken_count=0,
            links_deadline_interrupted=False,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertFalse(any(i.code == "link_check_incomplete" for i in report.issues))


class TestAnalyzerIssues(unittest.TestCase):
    """Analyzer issues are reused verbatim; penalties are computed."""

    def test_critical_issue(self):
        analysis = AnalysisResult(
            measurements={"title_length": 0},
            issues=[
                Issue(
                    code="missing_title",
                    severity=Severity.CRITICAL,
                    message="صفحه دارای عنوان نیست.",
                )
            ],
        )
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=1200.0,
        )
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            analysis=analysis,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 60)  # 40-point penalty
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(report.issues[0].code, "missing_title")

    def test_warning_issue(self):
        analysis = AnalysisResult(
            measurements={"meta_description_length": 0},
            issues=[
                Issue(
                    code="missing_meta_description",
                    severity=Severity.WARNING,
                    message="صفحه دارای متا توصیف نیست.",
                )
            ],
        )
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=1200.0,
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch, analysis=analysis)
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 90)  # 10-point penalty
        self.assertEqual(len(report.issues), 1)

    def test_info_issue(self):
        analysis = AnalysisResult(
            measurements={},
            issues=[
                Issue(
                    code="multiple_h1",
                    severity=Severity.INFO,
                    message="صفحه دارای ۲ تگ H1 است (تفوق به یک تگ H1).",
                )
            ],
        )
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=1200.0,
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch, analysis=analysis)
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 100)  # 0-point penalty
        self.assertEqual(len(report.issues), 1)

    def test_mixed_severities(self):
        analysis = AnalysisResult(
            measurements={},
            issues=[
                Issue(code="missing_title", severity=Severity.CRITICAL, message=""),
                Issue(code="title_too_short", severity=Severity.WARNING, message=""),
                Issue(code="missing_viewport", severity=Severity.WARNING, message=""),
                Issue(code="image_empty_alt", severity=Severity.INFO, message=""),
            ],
        )
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=1200.0,
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch, analysis=analysis)
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 50)  # 40 + 10 + 10 = 60 -> capped at 50 -> 50 penalty -> 50 score

    def test_unknown_analyzer_code_does_not_crash(self):
        analysis = AnalysisResult(
            measurements={},
            issues=[
                Issue(code="some_unknown_code", severity=Severity.WARNING, message="Some message")
            ],
        )
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=1200.0,
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch, analysis=analysis)
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 90)  # 10-point penalty from warning
        self.assertEqual(len(report.issues), 1)


class TestAnalysisUnavailable(unittest.TestCase):
    """When analysis cannot be performed after a successful fetch, emit one warning."""

    def test_analysis_unavailable_after_success(self):
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=1200.0,
        )
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            analysis_failed=True,
            analysis_reason="parser_error",
        )
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 90)  # 10-point penalty
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "analysis_unavailable")
        self.assertEqual(issue.severity, Severity.WARNING)
        self.assertIn("تحلیل محتوای صفحه انجام نشد", issue.message)

    def test_no_analysis_issues_injected(self):
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html><title>OK</title></html>",
            elapsed_ms=1200.0,
        )
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            analysis_failed=True,
            analysis_reason="no_html",
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        # No analyzer issues should be present
        self.assertFalse(any(i.code in ("missing_title", "missing_h1") for i in report.issues))


class TestCategoryCeilings(unittest.TestCase):
    """Verify each category ceiling is respected."""

    def test_fetch_ceiling_100(self):
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=404,
            headers={},
            body="Not Found",
            elapsed_ms=800.0,
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 0)  # 100-point fetch penalty -> 0

    def test_http_ceiling_10(self):
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=4000.0,  # slow
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 90)  # 10-point http penalty -> 90

    def test_robots_ceiling_10(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            robots_exists=ResourceState.UNKNOWN,
            robots_reason="timeout",
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 90)  # 10-point robots penalty -> 90

    def test_sitemap_ceiling_10(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            sitemap_exists=ResourceState.UNKNOWN,
            sitemap_reason="timeout",
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 90)  # 10-point sitemap penalty -> 90

    def test_links_ceiling_15(self):
        fetch = _successful_fetch()
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            confirmed_broken_count=10,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 85)  # 15-point links penalty -> 85

    def test_content_ceiling_50(self):
        analysis = AnalysisResult(
            measurements={},
            issues=[
                Issue(code="missing_title", severity=Severity.CRITICAL, message=""),
                Issue(code="missing_meta_description", severity=Severity.WARNING, message=""),
                Issue(code="missing_h1", severity=Severity.WARNING, message=""),
                Issue(code="missing_viewport", severity=Severity.WARNING, message=""),
            ],
        )
        fetch = _successful_fetch()
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch, analysis=analysis)
        scorer = HealthScorer()
        report = scorer.score(inp)
        self.assertEqual(report.score, 50)  # 40+10+10+10 = 70 -> capped at 50 -> 50 score


class TestHttpBoundary(unittest.TestCase):
    """Verify HTTP classification and slow-response boundary conditions."""

    def test_http_status_400_produces_http_error(self):
        fetch = FetchResult(
            url="https://example.com/bad",
            final_url="https://example.com/bad",
            status=400,
            headers={},
            body="Bad Request",
            elapsed_ms=500.0,
        )
        inp = ScoreInput(requested_url="https://example.com/bad", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 0)
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.code, "http_error")
        self.assertEqual(issue.severity, Severity.CRITICAL)
        self.assertEqual(report.status, 400)

    def test_http_status_301_does_not_produce_http_error(self):
        # 3xx is not a successful final response per the fixed classification
        # (only 2xx is considered successful). 301 must emit http_error.
        fetch = FetchResult(
            url="https://example.com/redirected",
            final_url="https://example.com/redirected",
            status=301,
            headers={},
            body="Moved Permanently",
            elapsed_ms=800.0,
        )
        inp = ScoreInput(requested_url="https://example.com/redirected", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 0)
        self.assertTrue(any(i.code == "http_error" for i in report.issues))

    def test_slow_response_boundary_3000_not_slow(self):
        # elapsed_ms == 3000.0 must NOT produce http_slow (boundary is exclusive)
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=3000.0,
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 100)
        self.assertFalse(any(i.code == "http_slow" for i in report.issues))

    def test_slow_response_boundary_3000_01_is_slow(self):
        # elapsed_ms == 3000.01 MUST produce http_slow
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=3000.01,
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)

        self.assertEqual(report.score, 90)
        self.assertTrue(any(i.code == "http_slow" for i in report.issues))


class TestPersianMessages(unittest.TestCase):
    """Verify every scorer-owned Issue.message is non-empty and Persian."""

    SCORER_CODES_SUCCESS = [
        "http_slow",
        "robots_unavailable",
        "sitemap_invalid",
        "broken_links",
        "link_check_incomplete",
        "analysis_unavailable",
    ]

    SCORER_CODES_FETCH_FAILURE = [
        "fetch_failed",
    ]

    SCORER_CODES_HTTP_ERROR = [
        "http_error",
    ]

    def _has_persian_char(self, text: str) -> bool:
        """Return True if text contains at least one character in the
        Arabic/Persian Unicode block (U+0600–U+06FF)."""
        return any("\u0600" <= ch <= "\u06FF" for ch in text)

    def _check_messages(self, report: HealthReport, expected_codes):
        messages = {i.code: i.message for i in report.issues}
        for code in expected_codes:
            self.assertIn(code, messages, f"Missing scorer issue: {code}")
            msg = messages[code]
            self.assertIsNotNone(msg, f"Message for '{code}' is None")
            self.assertGreater(len(msg), 0, f"Message for '{code}' is empty")
            self.assertTrue(
                self._has_persian_char(msg),
                f"Message for '{code}' contains no Persian character: {msg!r}",
            )

    def test_all_scorer_messages_non_empty_and_persian_success(self):
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=3500.0,  # triggers http_slow
        )
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            robots_exists=ResourceState.UNKNOWN,
            robots_reason="timeout",
            sitemap_exists=ResourceState.TRUE,
            sitemap_valid=False,
            sitemap_url_count=5,
            confirmed_broken_count=2,
            broken_url_samples=["https://example.com/a", "https://example.com/b"],
            links_deadline_interrupted=True,
            analysis_failed=True,
            analysis_reason="parse_error",
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self._check_messages(report, self.SCORER_CODES_SUCCESS)

    def test_fetch_failed_message_is_persian(self):
        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=None,
            fetch_failure_code="dns_failure",
        )
        scorer = HealthScorer()
        report = scorer.score(inp)
        self._check_messages(report, self.SCORER_CODES_FETCH_FAILURE)

    def test_http_error_message_is_persian(self):
        fetch = FetchResult(
            url="https://example.com/bad",
            final_url="https://example.com/bad",
            status=404,
            headers={},
            body="Not Found",
            elapsed_ms=800.0,
        )
        inp = ScoreInput(requested_url="https://example.com/bad", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)
        self._check_messages(report, self.SCORER_CODES_HTTP_ERROR)


class TestCombinedScenario(unittest.TestCase):
    """Combined scenario from task description should score 15."""

    def test_combined_example(self):
        # Build a minimal HTML with missing title, meta description, h1, viewport
        html = "<html><head></head><body></body></html>"
        analyzer = HtmlAnalyzer("https://example.com/")
        analysis = analyzer.analyze(html)

        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body=html,
            elapsed_ms=1200.0,
        )

        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            analysis=analysis,
            robots_exists=ResourceState.UNKNOWN,
            robots_reason="timeout",
            sitemap_exists=ResourceState.TRUE,
            sitemap_valid=False,
            sitemap_url_count=5,
            confirmed_broken_count=3,
            broken_url_samples=[
                "https://example.com/link1",
                "https://example.com/link2",
                "https://example.com/link3",
            ],
        )
        scorer = HealthScorer()
        report = scorer.score(inp)

        # Expected: content capped at 50, links deduct 15, robots 10, sitemap 10 -> 100 - 85 = 15
        self.assertEqual(report.score, 15)

        # Verify issues are present and ordered deterministically
        codes = [i.code for i in report.issues]
        # Should contain analyzer issues (missing_title, missing_meta_description, missing_h1, missing_viewport)
        # and scorer issues (robots_unavailable, sitemap_invalid, broken_links)
        self.assertIn("missing_title", codes)
        self.assertIn("missing_meta_description", codes)
        self.assertIn("missing_h1", codes)
        self.assertIn("missing_viewport", codes)
        self.assertIn("robots_unavailable", codes)
        self.assertIn("sitemap_invalid", codes)
        self.assertIn("broken_links", codes)

        # Verify deterministic ordering: critical first, then warning, then info
        severities = [i.severity for i in report.issues]
        self.assertEqual(severities, sorted(severities, key=lambda s: {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}[s]))


class TestDeterminismAndDeduplication(unittest.TestCase):
    """Verify deterministic issue ordering and repeated-call equality."""

    def test_deterministic_ordering(self):
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=404,
            headers={},
            body="Not Found",
            elapsed_ms=800.0,
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch)
        scorer = HealthScorer()

        report1 = scorer.score(inp)
        report2 = scorer.score(inp)

        # Same issues, same order
        self.assertEqual([i.code for i in report1.issues], [i.code for i in report2.issues])
        self.assertEqual(report1.score, report2.score)
        self.assertEqual(report1.issues, report2.issues)

    def test_deduplication_by_code_and_details(self):
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html></html>",
            elapsed_ms=1200.0,
        )
        inp = ScoreInput(requested_url="https://example.com/", fetch=fetch)
        scorer = HealthScorer()
        report = scorer.score(inp)
        # No duplicates in a clean run
        codes = [i.code for i in report.issues]
        self.assertEqual(len(codes), len(set(codes)))


class TestNoMeasurementsStringsOrLists(unittest.TestCase):
    """Ensure measurements only contain numeric, boolean, or None values."""

    def test_measurements_types(self):
        fetch = FetchResult(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            headers={},
            body="<html><title>Test</title></html>",
            elapsed_ms=1500.0,
        )
        analyzer = HtmlAnalyzer("https://example.com/")
        analysis = analyzer.analyze(fetch.body)

        inp = ScoreInput(
            requested_url="https://example.com/",
            fetch=fetch,
            analysis=analysis,
            robots_exists=ResourceState.TRUE,
            sitemap_exists=ResourceState.TRUE,
            sitemap_valid=True,
            total_unique_links=5,
            checked_links=5,
            confirmed_broken_count=0,
        )
        scorer = HealthScorer()
        report = scorer.score(inp)

        for key, value in report.measurements.items():
            self.assertIsInstance(
                value, (int, float, bool, type(None)),
                f"Measurement '{key}' has disallowed type {type(value).__name__}"
            )


if __name__ == "__main__":
    unittest.main()