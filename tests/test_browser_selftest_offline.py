"""Offline unit tests for FakeTube fixtures and browser selftest module.

Verifies:
- FakeTube HTML fixtures exist and parse as valid HTML.
- Fixture structure matches required selectors (ads, shorts, sign-in, videos, recommended).
- scripts.browser_selftest imports without side effects or launching a browser.
- PlaywrightBackend supports the allow_loopback constructor flag.
"""

from html.parser import HTMLParser
from pathlib import Path
import pytest

from browser.guards import check_url, is_interactive, is_signin_url, page_type
from browser.playwright_backend import PlaywrightBackend

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "fake_tube"


class SimpleHTMLValidator(HTMLParser):
    """Basic HTML parser to verify well-formedness and collect tags and attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.anchors: list[dict[str, str]] = []
        self.videos: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        attr_dict = {k: v or "" for k, v in attrs}
        if tag == "a":
            self.anchors.append(attr_dict)
        elif tag == "video":
            self.videos.append(attr_dict)


def test_faketube_fixtures_exist():
    """Verify all required FakeTube HTML fixture files exist on disk."""
    results_path = FIXTURES_DIR / "results.html"
    home_path = FIXTURES_DIR / "home.html"
    watch_path = FIXTURES_DIR / "watch.html"

    assert results_path.exists(), f"Missing {results_path}"
    assert home_path.exists(), f"Missing {home_path}"
    assert watch_path.exists(), f"Missing {watch_path}"


def test_faketube_fixtures_parse_as_html():
    """Verify all FakeTube fixtures parse cleanly as HTML without parsing errors."""
    for filename in ("results.html", "home.html", "watch.html"):
        file_path = FIXTURES_DIR / filename
        content = file_path.read_text(encoding="utf-8")
        validator = SimpleHTMLValidator()
        validator.feed(content)
        assert len(validator.tags) > 5, f"{filename} had unexpectedly few tags"


def test_faketube_results_fixture_structure():
    """Verify results.html contains an ad block, Shorts shelf, sign-in link, and 6 videos."""
    content = (FIXTURES_DIR / "results.html").read_text(encoding="utf-8")
    validator = SimpleHTMLValidator()
    validator.feed(content)

    # Check for ad block
    assert "promoted" in content or "ytd-promoted-video-renderer" in content

    # Check for Shorts shelf
    assert "ytd-reel-shelf-renderer" in content or "/shorts/" in content

    # Check for sign-in link
    signin_links = [a for a in validator.anchors if "/signin" in a.get("href", "")]
    assert len(signin_links) >= 1, "Expected at least one sign-in link"

    # Check for 6 standard video watch links
    watch_links = [a for a in validator.anchors if "/watch?v=vid" in a.get("href", "")]
    assert len(watch_links) == 6, f"Expected 6 video anchors, got {len(watch_links)}"


def test_faketube_watch_fixture_structure():
    """Verify watch.html contains a video element with data URI, secondary recommended videos, and sign-in link."""
    content = (FIXTURES_DIR / "watch.html").read_text(encoding="utf-8")
    validator = SimpleHTMLValidator()
    validator.feed(content)

    # Check for video element backed by data: source
    assert len(validator.videos) >= 1, "Expected at least one <video> element"
    video_src = validator.videos[0].get("src", "")
    assert video_src.startswith("data:"), f"Expected inline data URI video, got {video_src[:30]}"

    # Check for secondary column with recommended videos
    rec_links = [a for a in validator.anchors if "/watch?v=rec" in a.get("href", "")]
    assert len(rec_links) >= 2, f"Expected at least 2 recommended videos, got {len(rec_links)}"

    # Check for sign-in link
    signin_links = [a for a in validator.anchors if "/signin" in a.get("href", "")]
    assert len(signin_links) >= 1, "Expected sign-in link on watch page"


def test_browser_selftest_imports_without_side_effects():
    """Verify scripts.browser_selftest imports cleanly without running tests or servers."""
    import scripts.browser_selftest as selftest

    assert hasattr(selftest, "FakeTubeHandler")
    assert hasattr(selftest, "run_all_stages")
    assert hasattr(selftest, "run_stage")


def test_playwright_backend_allow_loopback_flag():
    """Verify PlaywrightBackend constructor accepts allow_loopback and defaults to False."""
    backend_default = PlaywrightBackend()
    assert backend_default.allow_loopback is False

    backend_test = PlaywrightBackend(allow_loopback=True)
    assert backend_test.allow_loopback is True


def test_guards_loopback_behavior():
    """Verify guards respect allow_loopback parameter appropriately."""
    # When allow_loopback is False: loopback is blocked
    with pytest.raises(Exception):
        check_url("http://127.0.0.1:8080/results", allow_loopback=False)

    # When allow_loopback is True: loopback is allowed
    clean_url = check_url("http://127.0.0.1:8080/results", allow_loopback=True)
    assert "127.0.0.1:8080/results" in clean_url

    # Private non-loopback addresses must be blocked even if allow_loopback is True
    with pytest.raises(Exception):
        check_url("http://192.168.1.1:8080", allow_loopback=True)

    # Page type classification on loopback
    assert page_type("http://127.0.0.1:8080/results", allow_loopback=True) == "youtube_results"
    assert page_type("http://127.0.0.1:8080/watch?v=123", allow_loopback=True) == "youtube_watch"
    assert page_type("http://127.0.0.1:8080/", allow_loopback=True) == "youtube_home"
    assert page_type("http://127.0.0.1:8080/results", allow_loopback=False) == "unknown"

    # Interactive check
    assert is_interactive("http://127.0.0.1:8080/results", allow_loopback=True) is True
    assert is_interactive("http://127.0.0.1:8080/results", allow_loopback=False) is False

    # Sign-in check on loopback
    assert is_signin_url("http://127.0.0.1:8080/signin", allow_loopback=True) is True
