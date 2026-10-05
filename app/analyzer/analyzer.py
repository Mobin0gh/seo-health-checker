"""HTML Analyzer for SEO Health Checker.

Analyzes HTML content to extract SEO measurements and discover internal links.
"""

from __future__ import annotations

import html.parser
import urllib.parse
from typing import Dict, List, Optional, Set, Tuple

from app.contracts import AnalysisResult, Issue, Severity


# SEO length thresholds (approved constants)
TITLE_TOO_SHORT_THRESHOLD = 30
TITLE_TOO_LONG_THRESHOLD = 60
META_DESCRIPTION_TOO_SHORT_THRESHOLD = 70
META_DESCRIPTION_TOO_LONG_THRESHOLD = 160


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _same_effective_host(
    parsed_a: urllib.parse.ParseResult,
    parsed_b: urllib.parse.ParseResult,
) -> bool:
    """Return True if two URLs share the same effective host.

    Hostnames are compared case-insensitively. Explicit default ports
    (80 for http, 443 for https) are treated as equivalent to an
    omitted port, so ``example.com`` and ``example.com:443`` match.
    Genuinely different ports (e.g. ``:8443``) are treated as
    different hosts.
    """
    if parsed_a.hostname is None or parsed_b.hostname is None:
        return False
    if parsed_a.hostname.lower() != parsed_b.hostname.lower():
        return False
    if parsed_a.scheme.lower() != parsed_b.scheme.lower():
        return False
    port_a = parsed_a.port
    port_b = parsed_b.port
    default = _DEFAULT_PORTS.get(parsed_a.scheme.lower())
    if port_a is not None and port_a == default:
        port_a = None
    if port_b is not None and port_b == default:
        port_b = None
    return port_a == port_b


class _HtmlAnalyzerParser(html.parser.HTMLParser):
    """Internal HTML parser that collects SEO-relevant data."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url

        # State tracking
        self._in_title = False
        self._in_script = False
        self._in_style = False
        self._in_noscript = False
        self._in_template = False

        # Title data
        self._title_parts: List[str] = []
        self._has_title = False
        self._title_text = ""

        # Meta description data
        self._meta_description_parts: List[str] = []
        self._has_meta_description = False
        self._meta_description_text = ""
        self._meta_description_seen = False

        # H1 data
        self._h1_count = 0
        self._in_h1 = False
        self._h1_parts: List[str] = []

        # Viewport data
        self._has_viewport = False

        # Image data
        self._image_count = 0
        self._image_alt_missing = 0
        self._image_alt_empty = 0

        # Canonical data
        self._has_canonical = False
        self._canonical_href = ""

        # Internal links data
        self._internal_links: List[str] = []
        self._seen_link_urls: Set[str] = set()

        # Measurements that will be populated
        self.html_size = 0

    # --- Handler methods ---

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """Process start tags and collect relevant data."""
        tag_lower = tag.lower()

        # Track script/style/noscript/template content to avoid false positives
        if tag_lower == "script":
            self._in_script = True
        elif tag_lower == "style":
            self._in_style = True
        elif tag_lower == "noscript":
            self._in_noscript = True
        elif tag_lower == "template":
            self._in_template = True

        # Skip processing if inside script/style/noscript/template
        if self._in_script or self._in_style or self._in_noscript or self._in_template:
            return

        # Process tags only when not in excluded contexts
        if tag_lower == "title":
            self._in_title = True
            self._title_parts = []
        elif tag_lower == "meta":
            self._process_meta_tag(dict(attrs))
        elif tag_lower == "h1":
            self._in_h1 = True
            self._h1_parts = []
            self._h1_count += 1
        elif tag_lower == "img":
            self._process_img_tag(dict(attrs))
        elif tag_lower == "link":
            self._process_link_tag(dict(attrs))
        elif tag_lower == "a":
            self._process_a_tag(dict(attrs))

    def handle_endtag(self, tag: str) -> None:
        """Process end tags and finalize data collection."""
        tag_lower = tag.lower()

        if tag_lower in ("script", "style"):
            self._in_script = False
            self._in_style = False
        elif tag_lower == "noscript":
            self._in_noscript = False
        elif tag_lower == "template":
            self._in_template = False

        if tag_lower == "title":
            self._in_title = False
            if self._title_parts:
                self._title_text = "".join(self._title_parts)
                self._has_title = True
        elif tag_lower == "h1":
            self._in_h1 = False
            if self._h1_parts:
                # We already counted the H1 in handle_starttag
                pass

    def handle_data(self, data: str) -> None:
        """Process text data within tags."""
        if self._in_script or self._in_style or self._in_noscript or self._in_template:
            return

        if self._in_title:
            self._title_parts.append(data)
        elif self._in_h1:
            self._h1_parts.append(data)

    # --- Private processing methods ---

    def _process_meta_tag(self, attrs: Dict[str, Optional[str]]) -> None:
        """Process meta tags for description and other metadata."""
        name = (attrs.get("name") or "").lower()
        property_attr = (attrs.get("property") or "").lower()
        content = attrs.get("content")

        # Check for meta viewport
        if name == "viewport":
            self._has_viewport = True

        # Check for meta description (name="description")
        if name == "description" and content is not None:
            if not self._meta_description_seen:
                self._has_meta_description = True
                self._meta_description_seen = True
                self._meta_description_parts = [content]
            # Ignore subsequent meta description tags (first wins)

    def _process_img_tag(self, attrs: Dict[str, Optional[str]]) -> None:
        """Process img tags for alt attribute analysis."""
        self._image_count += 1
        alt_attr = attrs.get("alt")

        if alt_attr is None:
            # Missing alt attribute
            self._image_alt_missing += 1
        else:
            # Check if alt is empty or whitespace-only
            if alt_attr.strip() == "":
                self._image_alt_empty += 1

    def _process_link_tag(self, attrs: Dict[str, Optional[str]]) -> None:
        """Process link tags for canonical URL."""
        rel = (attrs.get("rel") or "").lower()
        href = attrs.get("href")

        if rel == "canonical" and href is not None:
            if not self._has_canonical:
                self._has_canonical = True
                # Empty href should not be resolved
                if href.strip() == "":
                    self._canonical_href = ""
                else:
                    # Resolve relative URL against base_url
                    try:
                        self._canonical_href = urllib.parse.urljoin(self.base_url, href)
                    except Exception:
                        # If URL resolution fails, keep as empty to trigger empty_canonical issue
                        self._canonical_href = ""

    def _process_a_tag(self, attrs: Dict[str, Optional[str]]) -> None:
        """Process anchor tags for internal link discovery."""
        href = attrs.get("href")
        if href is None:
            return

        # Skip empty href
        if href.strip() == "":
            return

        href_lower = href.lower()
        # Skip excluded protocols
        if (
            href_lower.startswith("mailto:")
            or href_lower.startswith("javascript:")
            or href_lower.startswith("data:")
        ):
            return

        try:
            # Resolve relative URL against base_url
            absolute_url = urllib.parse.urljoin(self.base_url, href)
            parsed = urllib.parse.urlparse(absolute_url)

            # Reconstruct URL without fragment, keep query parameters
            clean_url = urllib.parse.urlunparse(
                (parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, "")
            )

            # Check if same host as base_url, normalizing default ports
            base_parsed = urllib.parse.urlparse(self.base_url)
            if _same_effective_host(parsed, base_parsed):
                # Deduplicate while preserving document order
                if clean_url not in self._seen_link_urls:
                    self._seen_link_urls.add(clean_url)
                    self._internal_links.append(clean_url)
        except Exception:
            # Skip malformed URLs
            pass

    # --- Public properties ---

    @property
    def title_length(self) -> int:
        """Length of title text after trimming whitespace."""
        return len(self._title_text.strip()) if self._has_title else 0

    @property
    def has_title(self) -> bool:
        """Whether a non-empty title exists."""
        return self._has_title and bool(self._title_text.strip())

    @property
    def meta_description_length(self) -> int:
        """Length of meta description text."""
        return len("".join(self._meta_description_parts)) if self._has_meta_description else 0

    @property
    def has_meta_description(self) -> bool:
        """Whether a meta description exists."""
        return self._has_meta_description

    @property
    def h1_count(self) -> int:
        """Number of H1 elements."""
        return self._h1_count

    @property
    def has_viewport(self) -> bool:
        """Whether viewport meta tag exists."""
        return self._has_viewport

    @property
    def image_count(self) -> int:
        """Total number of images."""
        return self._image_count

    @property
    def image_alt_missing(self) -> int:
        """Number of images missing alt attribute."""
        return self._image_alt_missing

    @property
    def image_alt_empty(self) -> int:
        """Number of images with empty alt attribute."""
        return self._image_alt_empty

    @property
    def has_canonical(self) -> bool:
        """Whether a canonical link exists."""
        return self._has_canonical

    @property
    def internal_links(self) -> List[str]:
        """Discovered internal links (deduplicated, preserving order)."""
        return self._internal_links.copy()

    def feed(self, data: str) -> None:
        """Feed HTML data to the parser and track size."""
        self.html_size += len(data)
        super().feed(data)


class HtmlAnalyzer:
    """HTML analyzer for SEO measurements and internal link discovery."""

    def __init__(self, base_url: str) -> None:
        """Initialize analyzer with base URL for resolving relative links.

        Args:
            base_url: The base URL used to resolve relative URLs in the HTML.
        """
        # Basic validation - ensure it's a non-empty string
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")

        self.base_url = base_url.strip()

    def analyze(self, html: str) -> AnalysisResult:
        """Analyze HTML content and return measurements, links, and issues.

        Args:
            html: The HTML content to analyze.

        Returns:
            AnalysisResult containing measurements, internal links, and issues.
        """
        # Defensive: non-string input is treated as empty HTML
        if not isinstance(html, str):
            html = ""

        parser = _HtmlAnalyzerParser(self.base_url)
        parser.feed(html)

        measurements: Dict[str, float | int | bool | None] = {
            "title_length": parser.title_length,
            "has_title": parser.has_title,
            "meta_description_length": parser.meta_description_length,
            "has_meta_description": parser.has_meta_description,
            "h1_count": parser.h1_count,
            "has_viewport": parser.has_viewport,
            "image_count": parser.image_count,
            "image_alt_missing": parser.image_alt_missing,
            "image_alt_empty": parser.image_alt_empty,
            "has_canonical": parser.has_canonical,
            "html_size": parser.html_size,
        }

        issues: List[Issue] = []

        # Title issues
        if not parser.has_title:
            issues.append(
                Issue(
                    code="missing_title",
                    severity=Severity.CRITICAL,
                    message="صفحه دارای عنوان نیست.",
                    details=None,
                )
            )
        else:
            title_len = parser.title_length
            if title_len < TITLE_TOO_SHORT_THRESHOLD:
                issues.append(
                    Issue(
                        code="title_too_short",
                        severity=Severity.WARNING,
                        message=f"عنوان خیلی کوتاه است ({title_len} کاراکتر).",
                        details=None,
                    )
                )
            elif title_len > TITLE_TOO_LONG_THRESHOLD:
                issues.append(
                    Issue(
                        code="title_too_long",
                        severity=Severity.WARNING,
                        message=f"عنوان خیلی طولانی است ({title_len} کاراکتر).",
                        details=None,
                    )
                )

        # Meta description issues
        if not parser.has_meta_description:
            issues.append(
                Issue(
                    code="missing_meta_description",
                    severity=Severity.WARNING,
                    message="صفحه دارای متا توصیف نیست.",
                    details=None,
                )
            )
        else:
            desc_len = parser.meta_description_length
            if desc_len < META_DESCRIPTION_TOO_SHORT_THRESHOLD:
                issues.append(
                    Issue(
                        code="meta_description_too_short",
                        severity=Severity.WARNING,
                        message=f"متا توصیف خیلی کوتاه است ({desc_len} کاراکتر).",
                        details=None,
                    )
                )
            elif desc_len > META_DESCRIPTION_TOO_LONG_THRESHOLD:
                issues.append(
                    Issue(
                        code="meta_description_too_long",
                        severity=Severity.WARNING,
                        message=f"متا توصیف خیلی طولانی است ({desc_len} کاراکتر).",
                        details=None,
                    )
                )

        # H1 issues
        if parser.h1_count == 0:
            issues.append(
                Issue(
                    code="missing_h1",
                    severity=Severity.WARNING,
                    message="صفحه دارای تگ H1 نیست.",
                    details=None,
                )
            )
        elif parser.h1_count > 1:
            issues.append(
                Issue(
                    code="multiple_h1",
                    severity=Severity.INFO,
                    message=f"صفحه دارای {parser.h1_count} تگ H1 است (تفوق به یک تگ H1).",
                    details=None,
                )
            )

        # Viewport issues
        if not parser.has_viewport:
            issues.append(
                Issue(
                    code="missing_viewport",
                    severity=Severity.WARNING,
                    message="صفحه دارای متا تگ viewport نیست.",
                    details=None,
                )
            )

        # Image alt issues
        if parser.image_alt_missing > 0:
            issues.append(
                Issue(
                    code="image_missing_alt",
                    severity=Severity.WARNING,
                    message=f"{parser.image_alt_missing} تصویر بدون ویژگی alt دارد.",
                    details=None,
                )
            )

        if parser.image_alt_empty > 0:
            issues.append(
                Issue(
                    code="image_empty_alt",
                    severity=Severity.INFO,
                    message=f"{parser.image_alt_empty} تصویر دارای ویژگی alt خالی است.",
                    details=None,
                )
            )

        # Canonical issues
        if not parser.has_canonical:
            issues.append(
                Issue(
                    code="missing_canonical",
                    severity=Severity.INFO,
                    message="صفحه دارای لینک کانونیکال نیست.",
                    details=None,
                )
            )
        elif not parser._canonical_href:
            issues.append(
                Issue(
                    code="empty_canonical",
                    severity=Severity.INFO,
                    message="لینک کانونیکال خالی است.",
                    details=None,
                )
            )

        return AnalysisResult(
            measurements=measurements,
            internal_links=parser.internal_links,
            issues=issues,
        )