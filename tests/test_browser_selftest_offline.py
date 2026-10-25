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

from browser.backend import FakeBackend
from browser.controller import BrowserController
from browser.guards import BrowserRefused, check_url, is_interactive, is_signin_url, page_type
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


def test_stage_6_signin_refusal_scenario_offline():
    """Reproduce Stage 6 sign-in refusal flow with fake backend without launching a real browser.

    Verifies:
    1. Clicking sign-in link navigates to mock /signin fixture on loopback when allowed.
    2. _check_signin_guard detects the sign-in URL, reverts history, and raises BrowserRefused.
    3. Reproduces the bug: if the route handler aborted navigation so URL remained on /watch,
       _check_signin_guard does not raise, triggering Stage 6's assertion failure.
    """
    backend = FakeBackend(
        pages={
            "http://127.0.0.1:8080/watch?v=rec1": {
                "title": "Watch - FakeTube",
                "items": ["First Recommended Video"],
            },
            "http://127.0.0.1:8080/signin": {
                "title": "Sign in - FakeTube",
                "items": [],
            },
        }
    )
    backend.allow_loopback = True
    controller = BrowserController(backend_factory=lambda: backend)

    # 1. Start on watch page
    controller.navigate("http://127.0.0.1:8080/watch?v=rec1")
    assert backend.current_url() == "http://127.0.0.1:8080/watch?v=rec1"

    # 2. Simulate clicking sign-in link: browser navigates to /signin
    backend.goto("http://127.0.0.1:8080/signin")
    assert backend.current_url() == "http://127.0.0.1:8080/signin"

    # 3. controller._check_signin_guard detects sign-in on loopback and raises BrowserRefused
    with pytest.raises(BrowserRefused) as exc_info:
        controller._check_signin_guard(backend)

    assert "asked to sign in" in str(exc_info.value)
    # Browser history was navigated back to the watch page
    assert backend.current_url() == "http://127.0.0.1:8080/watch?v=rec1"

    # 4. Bug reproduction: If route handler aborted navigation, the page remained on /watch
    backend._current_url = "http://127.0.0.1:8080/watch?v=rec1"
    # Calling _check_signin_guard on /watch does NOT raise BrowserRefused
    controller._check_signin_guard(backend)
    # This verifies why Stage 6 previously raised AssertionError("Clicking sign-in link was not refused!")


def test_selftest_stage_reply_validator():
    """Parse scripts/browser_selftest.py stages and verify every reply assertion matches a producible backend reply."""
    import ast

    selftest_path = Path(__file__).resolve().parent.parent / "scripts" / "browser_selftest.py"
    assert selftest_path.exists(), f"Missing {selftest_path}"
    content = selftest_path.read_text(encoding="utf-8")
    tree = ast.parse(content)

    # Collect producible strings from backend, controller, guards, and fixtures
    backend_path = Path(__file__).resolve().parent.parent / "browser" / "playwright_backend.py"
    controller_path = Path(__file__).resolve().parent.parent / "browser" / "controller.py"
    guards_path = Path(__file__).resolve().parent.parent / "browser" / "guards.py"
    backend_text = backend_path.read_text(encoding="utf-8")
    controller_text = controller_path.read_text(encoding="utf-8")
    guards_text = guards_path.read_text(encoding="utf-8")
    fixture_text = "".join(f.read_text(encoding="utf-8") for f in FIXTURES_DIR.glob("*.html"))

    valid_corpus = backend_text + controller_text + guards_text + fixture_text

    # Extract all stage functions from browser_selftest.py
    stages: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("stage_"):
            stage_name = node.name
            strings_to_check: list[str] = []
            for child in ast.walk(node):
                if isinstance(child, (ast.Assert, ast.If)):
                    test_expr = child.test
                    for sub in ast.walk(test_expr):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            val = sub.value.strip()
                            if val and val != "url":
                                strings_to_check.append(val)
            stages[stage_name] = strings_to_check

    assert len(stages) >= 6, f"Expected at least 6 stages, found {len(stages)}: {list(stages.keys())}"

    # Verify each string checked in assertions/conditions belongs to producible backend text or fixtures
    for stage_name, string_literals in stages.items():
        for s in string_literals:
            assert s in valid_corpus, (
                f"Stage '{stage_name}' asserts reply string {s!r} which is no longer producible by the backend!"
            )

    # Negative test: ensure the validator would fail on an invalid/obsolete string
    fake_obsolete_stage = ["Scrolled down 1 screen", "Video is playing.", "Sign-in wall reached"]
    for bad_string in fake_obsolete_stage:
        assert bad_string not in valid_corpus, f"Bad string {bad_string!r} should not be in valid corpus"

