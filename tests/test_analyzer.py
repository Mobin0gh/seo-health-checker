"""Unit tests for the HTML analyzer (app/analyzer/analyzer.py).

These tests verify that the analyzer correctly extracts SEO measurements,
discovers internal links, and generates appropriate issues for various HTML
content cases.

The test class inherits from unittest.TestCase so it can be run by pytest
(which discovers unittest tests) or directly via ``python -m unittest``.
"""

from __future__ import annotations

import unittest

from app.analyzer.analyzer import HtmlAnalyzer


class TestTitle(unittest.TestCase):
    """Test title analysis."""

    def setUp(self) -> None:
        self.analyzer = HtmlAnalyzer("https://example.com/")

    def test_missing_title(self):
        html = "<html><body><p>No title</p></body></html>"
        result = self.analyzer.analyze(html)
        self.assertFalse(result.measurements["has_title"])
        self.assertEqual(result.measurements["title_length"], 0)
        self.assertTrue(
            any(i.code == "missing_title" for i in result.issues)
        )

    def test_empty_title(self):
        html = "<html><head><title></title></head><body></body></html>"
        result = self.analyzer.analyze(html)
        self.assertFalse(result.measurements["has_title"])
        self.assertEqual(result.measurements["title_length"], 0)
        self.assertTrue(
            any(i.code == "missing_title" for i in result.issues)
        )

    def test_whitespace_title(self):
        html = "<html><head><title>   </title></head><body></body></html>"
        result = self.analyzer.analyze(html)
        self.assertFalse(result.measurements["has_title"])
        self.assertEqual(result.measurements["title_length"], 0)
        self.assertTrue(
            any(i.code == "missing_title" for i in result.issues)
        )

    def test_short_title(self):
        html = "<html><head><title>Hi</title></head><body></body></html>"
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_title"])
        self.assertEqual(result.measurements["title_length"], 2)
        self.assertTrue(
            any(i.code == "title_too_short" for i in result.issues)
        )

    def test_normal_title(self):
        # 30-60 characters
        title_text = "SEO Health Checker - Technical Audit Tool"
        html = f"<html><head><title>{title_text}</title></head><body></body></html>"
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_title"])
        self.assertEqual(result.measurements["title_length"], len(title_text))
        self.assertFalse(
            any(i.code == "title_too_short" for i in result.issues)
        )
        self.assertFalse(
            any(i.code == "title_too_long" for i in result.issues)
        )

    def test_long_title(self):
        title_text = "A" * 65  # > 60
        html = f"<html><head><title>{title_text}</title></head><body></body></html>"
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_title"])
        self.assertEqual(result.measurements["title_length"], 65)
        self.assertTrue(
            any(i.code == "title_too_long" for i in result.issues)
        )

    def test_title_trimmed(self):
        title_text = "  Valid Title  "
        html = f"<html><head><title>{title_text}</title></head><body></body></html>"
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_title"])
        # Length should be of trimmed title
        self.assertEqual(result.measurements["title_length"], len(title_text.strip()))


class TestMetaDescription(unittest.TestCase):
    """Test meta description analysis."""

    def setUp(self) -> None:
        self.analyzer = HtmlAnalyzer("https://example.com/")

    def test_missing_description(self):
        html = "<html><head></head><body></body></html>"
        result = self.analyzer.analyze(html)
        self.assertFalse(result.measurements["has_meta_description"])
        self.assertEqual(result.measurements["meta_description_length"], 0)
        self.assertTrue(
            any(i.code == "missing_meta_description" for i in result.issues)
        )

    def test_empty_description(self):
        html = '<html><head><meta name="description" content=""></head><body></body></html>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_meta_description"])
        self.assertEqual(result.measurements["meta_description_length"], 0)
        # Empty description still has meta_description tag, but length is 0 so it's "too short"
        self.assertTrue(
            any(i.code == "meta_description_too_short" for i in result.issues)
        )

    def test_short_description(self):
        html = '<html><head><meta name="description" content="Short"></head><body></body></html>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_meta_description"])
        self.assertEqual(result.measurements["meta_description_length"], 5)
        self.assertTrue(
            any(i.code == "meta_description_too_short" for i in result.issues)
        )

    def test_normal_description(self):
        desc = "This is a normal meta description that should pass the SEO checks without any issues."
        html = f'<html><head><meta name="description" content="{desc}"></head><body></body></html>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_meta_description"])
        self.assertEqual(result.measurements["meta_description_length"], len(desc))
        self.assertFalse(
            any(i.code == "meta_description_too_short" for i in result.issues)
        )
        self.assertFalse(
            any(i.code == "meta_description_too_long" for i in result.issues)
        )

    def test_long_description(self):
        desc = "A" * 170  # > 160
        html = f'<html><head><meta name="description" content="{desc}"></head><body></body></html>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_meta_description"])
        self.assertEqual(result.measurements["meta_description_length"], 170)
        self.assertTrue(
            any(i.code == "meta_description_too_long" for i in result.issues)
        )

    def test_case_insensitive_detection(self):
        desc = "This is a normal meta description that should pass the SEO checks."
        html = f'<html><head><meta NAME="DESCRIPTION" CONTENT="{desc}"></head><body></body></html>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_meta_description"])

    def test_duplicate_description_first_wins(self):
        first_desc = "First description that is long enough to pass."
        second_desc = "Second description that is also long enough."
        html = (
            f'<html><head>'
            f'<meta name="description" content="{second_desc}">'
            f'<meta name="description" content="{first_desc}">'
            f'</head><body></body></html>'
        )
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_meta_description"])
        # Should use first one in document order
        self.assertEqual(result.measurements["meta_description_length"], len(second_desc))


class TestH1(unittest.TestCase):
    """Test H1 analysis."""

    def setUp(self) -> None:
        self.analyzer = HtmlAnalyzer("https://example.com/")

    def test_zero_h1(self):
        html = "<html><body><p>No heading</p></body></html>"
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["h1_count"], 0)
        self.assertTrue(any(i.code == "missing_h1" for i in result.issues))

    def test_one_h1(self):
        html = "<html><body><h1>Main Heading</h1></body></html>"
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["h1_count"], 1)
        self.assertFalse(any(i.code == "missing_h1" for i in result.issues))
        self.assertFalse(any(i.code == "multiple_h1" for i in result.issues))

    def test_multiple_h1(self):
        html = "<html><body><h1>First</h1><h1>Second</h1></body></html>"
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["h1_count"], 2)
        issue = next(i for i in result.issues if i.code == "multiple_h1")
        self.assertEqual(issue.severity, "info")  # Should be info, not warning

    def test_case_insensitive_h1(self):
        html = "<html><body><H1>Main Heading</H1></body></html>"
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["h1_count"], 1)

    def test_h1_with_whitespace(self):
        html = "<html><body><h1>   </h1></body></html>"
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["h1_count"], 1)


class TestViewport(unittest.TestCase):
    """Test viewport meta tag analysis."""

    def setUp(self) -> None:
        self.analyzer = HtmlAnalyzer("https://example.com/")

    def test_viewport_present(self):
        html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body></body></html>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_viewport"])
        self.assertFalse(any(i.code == "missing_viewport" for i in result.issues))

    def test_viewport_missing(self):
        html = "<html><head></head><body></body></html>"
        result = self.analyzer.analyze(html)
        self.assertFalse(result.measurements["has_viewport"])
        self.assertTrue(any(i.code == "missing_viewport" for i in result.issues))

    def test_case_insensitive_viewport(self):
        html = '<html><head><meta NAME="VIEWPORT" content="width=device-width"></head><body></body></html>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_viewport"])


class TestImages(unittest.TestCase):
    """Test image alt attribute analysis."""

    def setUp(self) -> None:
        self.analyzer = HtmlAnalyzer("https://example.com/")

    def test_no_images(self):
        html = "<html><body><p>Text</p></body></html>"
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["image_count"], 0)
        self.assertEqual(result.measurements["image_alt_missing"], 0)
        self.assertEqual(result.measurements["image_alt_empty"], 0)

    def test_valid_alt(self):
        html = '<html><body><img src="img.jpg" alt="A description"></body></html>'
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["image_count"], 1)
        self.assertEqual(result.measurements["image_alt_missing"], 0)
        self.assertEqual(result.measurements["image_alt_empty"], 0)

    def test_missing_alt(self):
        html = '<html><body><img src="img.jpg"></body></html>'
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["image_count"], 1)
        self.assertEqual(result.measurements["image_alt_missing"], 1)
        self.assertEqual(result.measurements["image_alt_empty"], 0)
        self.assertTrue(any(i.code == "image_missing_alt" for i in result.issues))

    def test_empty_alt(self):
        html = '<html><body><img src="img.jpg" alt=""></body></html>'
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["image_count"], 1)
        self.assertEqual(result.measurements["image_alt_missing"], 0)
        self.assertEqual(result.measurements["image_alt_empty"], 1)
        self.assertTrue(any(i.code == "image_empty_alt" for i in result.issues))

    def test_whitespace_alt(self):
        html = '<html><body><img src="img.jpg" alt="   "></body></html>'
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["image_count"], 1)
        self.assertEqual(result.measurements["image_alt_missing"], 0)
        self.assertEqual(result.measurements["image_alt_empty"], 1)
        # Whitespace alt should be treated as empty, not missing
        self.assertFalse(any(i.code == "image_missing_alt" for i in result.issues))
        self.assertTrue(any(i.code == "image_empty_alt" for i in result.issues))

    def test_mixed_states(self):
        html = (
            '<html><body>'
            '<img src="img1.jpg" alt="Valid alt">'
            '<img src="img2.jpg">'
            '<img src="img3.jpg" alt="">'
            '<img src="img4.jpg" alt="   ">'
            '</body></html>'
        )
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["image_count"], 4)
        self.assertEqual(result.measurements["image_alt_missing"], 1)
        self.assertEqual(result.measurements["image_alt_empty"], 2)


class TestCanonical(unittest.TestCase):
    """Test canonical link analysis."""

    def setUp(self) -> None:
        self.analyzer = HtmlAnalyzer("https://example.com/")

    def test_missing_canonical(self):
        html = "<html><head></head><body></body></html>"
        result = self.analyzer.analyze(html)
        self.assertFalse(result.measurements["has_canonical"])
        self.assertTrue(any(i.code == "missing_canonical" for i in result.issues))

    def test_valid_absolute_canonical(self):
        html = '<html><head><link rel="canonical" href="https://example.com/page"></head><body></body></html>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_canonical"])
        self.assertFalse(any(i.code == "missing_canonical" for i in result.issues))

    def test_valid_relative_canonical(self):
        html = '<html><head><link rel="canonical" href="/page"></head><body></body></html>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_canonical"])

    def test_empty_canonical(self):
        html = '<html><head><link rel="canonical" href=""></head><body></body></html>'
        result = self.analyzer.analyze(html)
        # Empty href with canonical rel should still count as has_canonical
        # but should trigger empty_canonical issue
        self.assertTrue(result.measurements["has_canonical"])
        self.assertTrue(any(i.code == "empty_canonical" for i in result.issues))

    def test_malformed_canonical(self):
        html = '<html><head><link rel="canonical" href="not-a-url%"></head><body></body></html>'
        result = self.analyzer.analyze(html)
        # Malformed URL might still parse but resolution would fail
        # The behavior should be safe - no crash

    def test_case_insensitive_canonical(self):
        html = '<html><head><LINK REL="CANONICAL" HREF="https://example.com/page"></HEAD><BODY></BODY></HTML>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_canonical"])


class TestInternalLinks(unittest.TestCase):
    """Test internal link discovery and resolution."""

    def setUp(self) -> None:
        self.analyzer = HtmlAnalyzer("https://example.com/")

    def test_absolute_same_host(self):
        html = '<a href="https://example.com/page1">Link</a>'
        result = self.analyzer.analyze(html)
        self.assertIn("https://example.com/page1", result.internal_links)

    def test_relative_url(self):
        html = '<a href="page1">Link</a>'
        result = self.analyzer.analyze(html)
        self.assertIn("https://example.com/page1", result.internal_links)

    def test_root_relative_url(self):
        html = '<a href="/page1">Link</a>'
        result = self.analyzer.analyze(html)
        self.assertIn("https://example.com/page1", result.internal_links)

    def test_protocol_relative_same_host(self):
        html = '<a href="//example.com/page1">Link</a>'
        result = self.analyzer.analyze(html)
        # Protocol-relative URLs resolve to same scheme as base
        self.assertIn("https://example.com/page1", result.internal_links)

    def test_external_host_excluded(self):
        html = '<a href="https://other.com/page1">Link</a>'
        result = self.analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 0)

    def test_mailto_excluded(self):
        html = '<a href="mailto:test@example.com">Email</a>'
        result = self.analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 0)

    def test_javascript_excluded(self):
        html = '<a href="javascript:void(0)">Click</a>'
        result = self.analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 0)

    def test_data_excluded(self):
        html = '<a href="data:text/html,base64">Data</a>'
        result = self.analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 0)

    def test_duplicate_links_deduplicated(self):
        html = (
            '<a href="/page1">Link 1</a>'
            '<a href="/page1">Link 2</a>'
            '<a href="https://example.com/page1">Link 3</a>'
        )
        result = self.analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 1)
        self.assertEqual(result.internal_links, ["https://example.com/page1"])

    def test_document_order_preserved(self):
        html = (
            '<a href="/page1">Link 1</a>'
            '<a href="/page2">Link 2</a>'
            '<a href="/page3">Link 3</a>'
        )
        result = self.analyzer.analyze(html)
        self.assertEqual(result.internal_links, [
            "https://example.com/page1",
            "https://example.com/page2",
            "https://example.com/page3",
        ])

    def test_empty_href_excluded(self):
        html = (
            '<a href="">Empty</a>'
            '<a href="/page1">Valid</a>'
        )
        result = self.analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 1)

    def test_query_strings_in_links(self):
        html = '<a href="/page1?param=value">Link</a>'
        result = self.analyzer.analyze(html)
        self.assertIn("https://example.com/page1?param=value", result.internal_links)

    def test_fragments_in_links(self):
        html = '<a href="/page1#section">Link</a>'
        result = self.analyzer.analyze(html)
        self.assertIn("https://example.com/page1", result.internal_links)

    def test_no_links(self):
        html = "<html><body><p>No links</p></body></html>"
        result = self.analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 0)


class TestRobustness(unittest.TestCase):
    """Test analyzer robustness with malformed and edge-case HTML."""

    def setUp(self) -> None:
        self.analyzer = HtmlAnalyzer("https://example.com/")

    def test_malformed_html(self):
        html = "<html><head><title>Test<</title><body><p>Unclosed"
        result = self.analyzer.analyze(html)
        # Should not crash; title length should be reasonable
        self.assertGreater(result.measurements["title_length"], 0)

    def test_entities_in_title(self):
        html = "<html><head><title>&lt;Test&gt;</title></head><body></body></html>"
        result = self.analyzer.analyze(html)
        # HTMLParser with convert_charrefs should convert entities
        self.assertGreater(result.measurements["title_length"], 0)

    def test_entities_in_description(self):
        html = '<html><head><meta name="description" content="A &amp; B description that is long enough."></head><body></body></html>'
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_meta_description"])

    def test_nested_html(self):
        html = (
            "<html><head><title>Nested</title></head>"
            "<body><div><span><p>Content</p></span></div></body></html>"
        )
        result = self.analyzer.analyze(html)
        self.assertTrue(result.measurements["has_title"])

    def test_script_style_ignored(self):
        html = (
            "<html><head><title>Test</title></head>"
            "<body><script>alert('h1')</script>"
            "<style>h1 { color: red; }</style>"
            "<h1>Actual H1</h1></body></html>"
        )
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["h1_count"], 1)

    def test_empty_html(self):
        result = self.analyzer.analyze("")
        # Should not crash
        self.assertFalse(result.measurements["has_title"])
        self.assertEqual(result.measurements["h1_count"], 0)

    def test_plain_text_no_html_tags(self):
        result = self.analyzer.analyze("Just some plain text")
        # Should not crash
        self.assertFalse(result.measurements["has_title"])
        self.assertEqual(result.measurements["h1_count"], 0)

    def test_noscript_content_ignored(self):
        html = (
            "<html><head><title>Test</title></head>"
            "<body><noscript><h1>Fake H1</h1></noscript>"
            "<h1>Real H1</h1></body></html>"
        )
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["h1_count"], 1)

    def test_template_content_ignored(self):
        html = (
            "<html><head><title>Test</title></head>"
            "<body><template><img src='x.jpg'></template>"
            "<img src='real.jpg' alt='real'></body></html>"
        )
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["image_count"], 1)


class TestMeasurements(unittest.TestCase):
    """Test that measurements have correct types and stable values."""

    def setUp(self) -> None:
        self.analyzer = HtmlAnalyzer("https://example.com/")

    def test_measurements_types(self):
        html = (
            "<html><head><title>Test Title</title>"
            '<meta name="description" content="Test description.">'
            '<meta name="viewport" content="width=device-width">'
            '<link rel="canonical" href="https://example.com/canonical">'
            "</head><body>"
            '<img src="img.jpg" alt="Alt">'
            '<h1>Heading</h1>'
            '<a href="/page1">Link</a>'
            "<p>Some content here</p>"
            "</body></html>"
        )
        result = self.analyzer.analyze(html)

        # Check required measurements exist
        required = [
            "title_length", "has_title", "meta_description_length",
            "has_meta_description", "h1_count", "has_viewport",
            "image_count", "image_alt_missing", "image_alt_empty",
            "has_canonical", "html_size"
        ]
        for key in required:
            self.assertIn(key, result.measurements, f"Missing measurement: {key}")

        # Check types
        self.assertIsInstance(result.measurements["title_length"], int)
        self.assertIsInstance(result.measurements["has_title"], bool)
        self.assertIsInstance(result.measurements["meta_description_length"], int)
        self.assertIsInstance(result.measurements["has_meta_description"], bool)
        self.assertIsInstance(result.measurements["h1_count"], int)
        self.assertIsInstance(result.measurements["has_viewport"], bool)
        self.assertIsInstance(result.measurements["image_count"], int)
        self.assertIsInstance(result.measurements["image_alt_missing"], int)
        self.assertIsInstance(result.measurements["image_alt_empty"], int)
        self.assertIsInstance(result.measurements["has_canonical"], bool)
        self.assertIsInstance(result.measurements["html_size"], int)

    def test_html_size_matches_input_length(self):
        html = "<html><body><p>Test</p></body></html>"
        result = self.analyzer.analyze(html)
        self.assertEqual(result.measurements["html_size"], len(html))

    def test_no_raw_strings_in_measurements(self):
        html = (
            "<html><head><title>My Title Text</title>"
            '<link rel="canonical" href="https://example.com/canonical">'
            "</head><body><p>Content</p></body></html>"
        )
        result = self.analyzer.analyze(html)

        # No raw text strings should be in measurements
        for key, value in result.measurements.items():
            self.assertNotIsInstance(
                value, str,
                f"Measurement '{key}' contains raw string, not allowed"
            )


class TestBaseUrlValidation(unittest.TestCase):
    """Test base URL validation."""

    def test_empty_base_url_rejected(self):
        with self.assertRaises(ValueError):
            HtmlAnalyzer("")

    def test_whitespace_base_url_rejected(self):
        with self.assertRaises(ValueError):
            HtmlAnalyzer("  ")


class TestRegression(unittest.TestCase):
    """Regression tests for confirmed Task 3 review findings."""

    def setUp(self) -> None:
        self.analyzer = HtmlAnalyzer("https://example.com/")

    def test_none_input_does_not_crash(self):
        """HtmlAnalyzer.analyze(None) must not crash."""
        result = self.analyzer.analyze(None)
        self.assertFalse(result.measurements["has_title"])
        self.assertEqual(result.measurements["h1_count"], 0)
        self.assertEqual(len(result.internal_links), 0)

    def test_non_string_input_does_not_crash(self):
        """Non-string input should be treated as empty HTML."""
        for bad in (None, 123, [], {}, b"<html>"):
            with self.subTest(value=bad):
                result = self.analyzer.analyze(bad)
                self.assertFalse(result.measurements["has_title"])

    def test_default_port_matching_base_no_port_link_with_port(self):
        """base: https://example.com/  link: https://example.com:443/page"""
        html = '<a href="https://example.com:443/page">L</a>'
        result = self.analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 1)
        self.assertIn("https://example.com:443/page", result.internal_links)

    def test_default_port_matching_base_with_port_link_no_port(self):
        """base: https://example.com:443/  link: https://example.com/page"""
        analyzer = HtmlAnalyzer("https://example.com:443/")
        html = '<a href="https://example.com/page">L</a>'
        result = analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 1)
        self.assertIn("https://example.com/page", result.internal_links)

    def test_different_port_not_matched(self):
        """base: https://example.com/  link: https://example.com:8443/page"""
        html = '<a href="https://example.com:8443/page">L</a>'
        result = self.analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 0)

    def test_http_default_port_matching(self):
        """base: http://example.com/  link: http://example.com:80/page"""
        analyzer = HtmlAnalyzer("http://example.com/")
        html = '<a href="http://example.com:80/page">L</a>'
        result = analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 1)

    def test_different_scheme_not_matched(self):
        """base: https://example.com/  link: http://example.com/page"""
        html = '<a href="http://example.com/page">L</a>'
        result = self.analyzer.analyze(html)
        self.assertEqual(len(result.internal_links), 0)


if __name__ == "__main__":
    unittest.main()
