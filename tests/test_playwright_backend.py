"""Tests for PlaywrightBackend using an in-memory Playwright stub.

Verifies:
1. Lazy import (module imports without Playwright installed).
2. Route handler aborts private URLs and permits public ones.
3. Dialogs are automatically dismissed.
4. Downloads are disabled.
5. Popups become the active page and the 6th page closes the oldest.
6. Every page.evaluate call uses a script from the module's constant set.
7. Selector lookup per site with fallback.
8. Click refuses a result whose href is javascript: or a private address.
9. Driver errors map to friendly BrowserErrors.
"""

import sys
import types
from typing import Any, Callable, Optional
import pytest

from browser.guards import BrowserError, BrowserRefused, BrowserTimeout
from browser.playwright_backend import (
    _ALLOWED_EVALUATE_SCRIPTS,
    _GET_SCROLL_POS_SCRIPT,
    _GET_VIDEO_STATE_SCRIPT,
    _HAS_VIDEO_SCRIPT,
    _MEDIA_SCRIPTS,
    _SCROLL_BY_SCRIPT,
    _TAG_RESULTS_SCRIPT,
    PlaywrightBackend,
)


class FakeDialog:
    """Fake Playwright dialog object."""

    def __init__(self, message: str = "Test prompt"):
        self.message = message
        self.dismissed: bool = False

    def dismiss(self) -> None:
        self.dismissed = True


class FakeRequest:
    """Fake Playwright Request object."""

    def __init__(self, url: str, resource_type: str = "document", is_navigation: bool = True):
        self.url = url
        self.resource_type = resource_type
        self._is_navigation = is_navigation

    def is_navigation_request(self) -> bool:
        return self._is_navigation


class FakeRoute:
    """Fake Playwright Route object."""

    def __init__(self, url: str, resource_type: str = "document", is_navigation: bool = True):
        self.request = FakeRequest(url, resource_type=resource_type, is_navigation=is_navigation)
        self.aborted: bool = False
        self.continued: bool = False

    def abort(self) -> None:
        self.aborted = True

    def continue_(self) -> None:
        self.continued = True


class FakeLocatorItem:
    """Fake individual element inside a locator."""

    def __init__(self, visible: bool = True, href: Optional[str] = None, on_click: Optional[Callable] = None):
        self.visible = visible
        self.href = href
        self.clicked: bool = False
        self.on_click = on_click

    def is_visible(self) -> bool:
        return self.visible

    def get_attribute(self, name: str) -> Optional[str]:
        if name == "href":
            return self.href
        return None

    def locator(self, sel: str) -> "FakeLocator":
        return FakeLocator([])

    def click(self) -> None:
        self.clicked = True
        if self.on_click:
            self.on_click()


class FakeLocator:
    """Fake Playwright Locator."""

    def __init__(self, items: list[FakeLocatorItem]):
        self.items = items

    def count(self) -> int:
        return len(self.items)

    def nth(self, i: int) -> FakeLocatorItem:
        return self.items[i]

    @property
    def first(self) -> "FakeLocator":
        return FakeLocator(self.items[:1])


class FakeMouse:
    def __init__(self, page: Optional["FakePage"] = None):
        self.page = page
        self.moves: list[tuple[float, float]] = []
        self.wheels: list[tuple[float, float]] = []

    def move(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    def wheel(self, delta_x: float, delta_y: float) -> None:
        self.wheels.append((delta_x, delta_y))
        if self.page:
            current_y = self.page.scroll_pos.get("y", 0)
            inner_h = self.page.scroll_pos.get("innerHeight", 800)
            scroll_h = self.page.scroll_pos.get("scrollHeight", 1000)
            max_y = self.page.scroll_pos.get("max_y", scroll_h - inner_h)
            new_y = max(0, min(max_y, current_y + int(delta_y)))
            self.page.scroll_pos["y"] = new_y


class FakeKeyboard:
    def __init__(self, page: Optional["FakePage"] = None):
        self.page = page
        self.presses: list[str] = []

    def press(self, key: str) -> None:
        self.presses.append(key)
        if key == "k" and self.page and getattr(self.page, "k_starts_playback", False):
            self.page.video_state["paused"] = False
        elif key == "Home" and self.page:
            self.page.scroll_pos["y"] = 0
        elif key == "End" and self.page:
            inner_h = self.page.scroll_pos.get("innerHeight", 800)
            scroll_h = self.page.scroll_pos.get("scrollHeight", 1000)
            max_y = self.page.scroll_pos.get("max_y", scroll_h - inner_h)
            self.page.scroll_pos["y"] = max_y


class FakePage:
    """Fake Playwright Page."""

    def __init__(self, url: str = "about:blank", title: str = "Test Title"):
        self.url: str = url
        self._title: str = title
        self.is_closed_val: bool = False
        self.event_handlers: dict[str, list[Callable]] = {}
        self.evaluated_scripts: list[str] = []
        self.locators: dict[str, list[FakeLocatorItem]] = {}
        self.viewport_size: dict[str, int] = {"width": 1280, "height": 800}
        self.mouse: FakeMouse = FakeMouse(page=self)
        self.keyboard: FakeKeyboard = FakeKeyboard(page=self)
        self.raise_on_goto: Optional[Exception] = None
        self.front_brought: bool = False
        self.history_actions: list[str] = []
        self.tag_results_override: Optional[dict[str, Any]] = None
        self.video_state: dict[str, Any] = {"paused": True, "muted": False, "currentTime": 0.0}
        self.autoplay_blocked: bool = False
        self.k_starts_playback: bool = False
        self.scroll_pos: dict[str, int] = {"y": 0, "innerHeight": 800, "scrollHeight": 1000}
        self.has_video_val: bool = False

    def on(self, event: str, handler: Callable) -> None:
        self.event_handlers.setdefault(event, []).append(handler)

    def trigger(self, event: str, *args: Any, **kwargs: Any) -> None:
        for handler in self.event_handlers.get(event, []):
            handler(*args, **kwargs)

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 20000) -> None:
        if self.raise_on_goto:
            raise self.raise_on_goto
        self.url = url

    def bring_to_front(self) -> None:
        self.front_brought = True

    def title(self) -> str:
        return self._title

    def locator(self, selector: str) -> FakeLocator:
        if selector in self.locators:
            return FakeLocator(self.locators[selector])
        if selector == "[data-orbit-result]":
            tag_data = self.tag_results_override or {
                "count": 2,
                "items": [
                    {"index": 1, "title": "Result 1", "href": "https://www.youtube.com/watch?v=vid1"},
                    {"index": 2, "title": "Result 2", "href": "https://www.youtube.com/watch?v=vid2"},
                ],
            }
            items = []
            for it in tag_data.get("items", []):
                h = it.get("href")
                def _make_click(target=h):
                    def _click():
                        if target:
                            self.url = target
                    return _click
                items.append(FakeLocatorItem(visible=True, href=h, on_click=_make_click(h)))
            return FakeLocator(items)
        return FakeLocator([])

    def wait_for_selector(self, selector: str, timeout: int = 5000) -> None:
        pass

    def evaluate(self, script: str, *args: Any) -> Any:
        self.evaluated_scripts.append(script)
        if script == _TAG_RESULTS_SCRIPT:
            if self.tag_results_override is not None:
                return self.tag_results_override
            return {
                "count": 2,
                "items": [
                    {"index": 1, "title": "Result 1", "href": "https://www.youtube.com/watch?v=vid1"},
                    {"index": 2, "title": "Result 2", "href": "https://www.youtube.com/watch?v=vid2"},
                ],
            }
        if script == _GET_VIDEO_STATE_SCRIPT:
            return dict(self.video_state)
        if script == _GET_SCROLL_POS_SCRIPT:
            return dict(self.scroll_pos)
        if script == _HAS_VIDEO_SCRIPT:
            return self.has_video_val
        if script == _SCROLL_BY_SCRIPT:
            if args:
                delta = int(args[0])
                current_y = self.scroll_pos.get("y", 0)
                inner_h = self.scroll_pos.get("innerHeight", 800)
                scroll_h = self.scroll_pos.get("scrollHeight", 1000)
                max_y = self.scroll_pos.get("max_y", scroll_h - inner_h)
                new_y = max(0, min(max_y, current_y + delta))
                self.scroll_pos["y"] = new_y
            return None
        if script in _MEDIA_SCRIPTS.values():
            if script == _MEDIA_SCRIPTS.get("play"):
                if not getattr(self, "autoplay_blocked", False):
                    self.video_state["paused"] = False
            elif script == _MEDIA_SCRIPTS.get("pause"):
                self.video_state["paused"] = True
            elif script == _MEDIA_SCRIPTS.get("mute"):
                self.video_state["muted"] = True
            elif script == _MEDIA_SCRIPTS.get("unmute"):
                self.video_state["muted"] = False
            elif script == _MEDIA_SCRIPTS.get("toggle"):
                if not getattr(self, "autoplay_blocked", False) or not self.video_state.get("paused", True):
                    self.video_state["paused"] = not self.video_state.get("paused", True)
            return {"status": "ok"}
        if "results.slice" in script:
            return {
                "title": self._title,
                "url": self.url,
                "items": ["Result 1", "Result 2"],
            }
        return {}

    def wait_for_load_state(self, state: str, timeout: int = 20000) -> None:
        pass

    def is_closed(self) -> bool:
        return self.is_closed_val

    def close(self) -> None:
        self.is_closed_val = True
        self.trigger("close")

    def go_back(self, **kwargs: Any) -> None:
        self.history_actions.append("back")

    def go_forward(self, **kwargs: Any) -> None:
        self.history_actions.append("forward")

    def reload(self, **kwargs: Any) -> None:
        self.history_actions.append("reload")


class FakeBrowserContext:
    """Fake Playwright BrowserContext."""

    def __init__(self, **launch_kwargs: Any):
        self.launch_kwargs = launch_kwargs
        self.pages: list[FakePage] = [FakePage()]
        self.route_handlers: list[tuple[str, Callable]] = []
        self.event_handlers: dict[str, list[Callable]] = {}
        self.is_closed_val: bool = False

    def route(self, pattern: str, handler: Callable) -> None:
        self.route_handlers.append((pattern, handler))

    def on(self, event: str, handler: Callable) -> None:
        self.event_handlers.setdefault(event, []).append(handler)

    def trigger(self, event: str, *args: Any, **kwargs: Any) -> None:
        for handler in self.event_handlers.get(event, []):
            handler(*args, **kwargs)

    def new_page(self) -> FakePage:
        p = FakePage()
        self.pages.append(p)
        self.trigger("page", p)
        return p

    def is_closed(self) -> bool:
        return self.is_closed_val

    def close(self) -> None:
        self.is_closed_val = True
        for p in self.pages:
            p.close()


class FakeChromium:
    """Fake Chromium launcher."""

    def __init__(self):
        self.last_context: Optional[FakeBrowserContext] = None
        self.raise_on_launch: Optional[Exception] = None

    def launch_persistent_context(self, chromium_sandbox: Optional[bool] = None, **kwargs: Any) -> FakeBrowserContext:
        if self.raise_on_launch:
            raise self.raise_on_launch
        kwargs["chromium_sandbox"] = chromium_sandbox
        ctx = FakeBrowserContext(**kwargs)
        self.last_context = ctx
        return ctx


class FakePlaywrightInstance:
    """Fake Playwright root instance."""

    def __init__(self):
        self.chromium = FakeChromium()
        self.stopped: bool = False

    def start(self) -> "FakePlaywrightInstance":
        return self

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_playwright_stub(monkeypatch):
    """Inject a fake playwright stub into sys.modules and return the fake instance."""
    instance = FakePlaywrightInstance()

    playwright_mod = types.ModuleType("playwright")
    sync_api_mod = types.ModuleType("playwright.sync_api")
    sync_api_mod.sync_playwright = lambda: instance

    monkeypatch.setitem(sys.modules, "playwright", playwright_mod)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api_mod)

    return instance


# --- Test 1: Lazy Import ---

def test_lazy_import_without_playwright(monkeypatch, tmp_path):
    """The module imports cleanly and start() raises a friendly BrowserError when playwright is missing."""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    assert backend.is_alive() is False

    with pytest.raises(BrowserError) as exc_info:
        backend.start()

    assert "Playwright is not installed" in str(exc_info.value)
    assert "requirements-browser.txt" in str(exc_info.value)


# --- Test 2: Route Handler Blocks Private and Allows Public ---

def test_route_handler_aborts_private_allows_public(fake_playwright_stub, tmp_path):
    """Route handler intercepts network requests, aborting private/loopback and continuing public."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    assert len(ctx.route_handlers) == 1
    pattern, handler = ctx.route_handlers[0]
    assert pattern == "**/*"

    # Private and loopback requests must be aborted
    private_urls = [
        "http://127.0.0.1:8080/secret",
        "http://localhost:3000/",
        "http://192.168.1.1/admin",
        "http://10.0.0.5/api",
        "http://169.254.169.254/latest/meta-data",
        "http://myhost.local/",
        "http://internal.service.internal/",
    ]
    for p_url in private_urls:
        route = FakeRoute(p_url)
        handler(route)
        assert route.aborted is True, f"Failed to abort private URL: {p_url}"
        assert route.continued is False

    # Public web requests must be allowed
    public_urls = [
        "https://www.youtube.com/watch?v=123",
        "https://www.google.com/search?q=test",
        "https://en.wikipedia.org/wiki/Python",
        "https://github.com/orbit",
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
    ]
    for pub_url in public_urls:
        route = FakeRoute(pub_url)
        handler(route)
        assert route.continued is True, f"Failed to continue public URL: {pub_url}"
        assert route.aborted is False

    backend.close()


# --- Test 3: Dialogs are Dismissed ---

def test_dialogs_are_dismissed(fake_playwright_stub, tmp_path):
    """Every modal dialog (alert, confirm, prompt) is automatically dismissed."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    page = ctx.pages[0]

    # Trigger a dialog event on the page
    dialog = FakeDialog("Unexpected alert")
    page.trigger("dialog", dialog)

    assert dialog.dismissed is True

    backend.close()


# --- Test 4: Downloads are Disabled ---

def test_downloads_are_disabled(fake_playwright_stub, tmp_path):
    """Persistent context is launched with accept_downloads=False."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    assert ctx.launch_kwargs.get("accept_downloads") is False

    backend.close()


# --- Test 5: Popups Tracked & 6th Page Closes Oldest ---

def test_popups_and_page_capping(fake_playwright_stub, tmp_path):
    """New popup becomes active page, and exceeding 5 open pages closes the oldest."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    initial_page = ctx.pages[0]
    assert backend._active_page is initial_page
    assert len(backend._pages) == 1

    # Spawn 4 additional pages -> total 5
    page2 = ctx.new_page()
    assert backend._active_page is page2
    page3 = ctx.new_page()
    page4 = ctx.new_page()
    page5 = ctx.new_page()

    assert len(backend._pages) == 5
    assert initial_page.is_closed_val is False

    # Spawn 6th page -> oldest (initial_page) must be closed
    page6 = ctx.new_page()
    assert len(backend._pages) == 5
    assert initial_page.is_closed_val is True
    assert backend._active_page is page6
    assert initial_page not in backend._pages
    assert backend._pages == [page2, page3, page4, page5, page6]

    backend.close()


# --- Test 6: Script Immutability in Evaluate ---

def test_page_evaluate_uses_module_constants_only(fake_playwright_stub, tmp_path):
    """All page.evaluate calls strictly use predefined module-level constant scripts."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    page = ctx.pages[0]
    page.url = "https://www.youtube.com/watch?v=123"
    page.has_video_val = True

    # Test all media actions
    for action in ("play", "pause", "toggle", "mute", "unmute"):
        backend.media(action)

    # Test read_page
    backend.read_page()

    # Verify that every evaluated script belongs to _ALLOWED_EVALUATE_SCRIPTS
    assert len(page.evaluated_scripts) > 0
    for script in page.evaluated_scripts:
        assert script in _ALLOWED_EVALUATE_SCRIPTS

    backend.close()


# --- Test 7: Result selection by href and fewer than N results refusal ---

def test_click_result_verified_outcome_and_count_refusal(fake_playwright_stub, tmp_path):
    """click_result verifies URL page_type change and returns 'Opened result <n>: <title>', refusing if fewer than N results."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    page = ctx.pages[0]

    # YouTube results page
    page.url = "https://www.youtube.com/results?search_query=lofi"
    page.tag_results_override = {
        "count": 2,
        "items": [
            {"index": 1, "title": "Lofi Hip Hop Radio", "href": "https://www.youtube.com/watch?v=aaa"},
            {"index": 2, "title": "Chill Beats", "href": "https://www.youtube.com/watch?v=bbb"},
        ],
    }

    res = backend.click_result(2)
    assert res == "Opened result 2: Chill Beats"
    assert page.url == "https://www.youtube.com/watch?v=bbb"

    # Refuse if fewer than N results exist
    with pytest.raises(BrowserRefused, match="I only found 2 results on this page."):
        backend.click_result(3)

    backend.close()


# --- Test 8: Click Refuses Dangerous or Private Destination Hrefs ---

def test_click_refuses_javascript_and_private_addresses(fake_playwright_stub, tmp_path):
    """click_result refuses links pointing to javascript: or loopback/private hosts."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    page = ctx.pages[0]
    page.url = "https://www.google.com/search?q=test"

    # javascript: scheme
    page.tag_results_override = {
        "count": 1,
        "items": [{"index": 1, "title": "Dangerous", "href": "javascript:alert(1)"}],
    }
    with pytest.raises(BrowserRefused) as exc_info:
        backend.click_result(1)
    assert "scheme" in str(exc_info.value).lower() or "javascript:" in str(exc_info.value).lower()

    # localhost target
    page.tag_results_override = {
        "count": 1,
        "items": [{"index": 1, "title": "Local", "href": "http://127.0.0.1:8080/dump"}],
    }
    with pytest.raises(BrowserRefused) as exc_info:
        backend.click_result(1)
    assert "blocked" in str(exc_info.value).lower() or "loopback" in str(exc_info.value).lower()

    # Private IP target
    page.tag_results_override = {
        "count": 1,
        "items": [{"index": 1, "title": "Private", "href": "http://192.168.1.1/setup"}],
    }
    with pytest.raises(BrowserRefused) as exc_info:
        backend.click_result(1)
    assert "blocked" in str(exc_info.value).lower() or "private" in str(exc_info.value).lower()

    backend.close()


# --- Test 9: Friendly Driver Error Mapping ---

def test_driver_errors_map_to_friendly_browser_errors(fake_playwright_stub, tmp_path):
    """Driver errors (missing browser, closed target, timeout) map to user-friendly BrowserErrors."""
    # Subtest A: Missing Chrome
    fake_playwright_stub.chromium.raise_on_launch = Exception(
        "Executable doesn't exist at C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe. Google Chrome was not found."
    )
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    with pytest.raises(BrowserError) as exc_info:
        backend.start()
    assert "Google Chrome was not found" in str(exc_info.value)
    assert "playwright install chromium" in str(exc_info.value)

    # Subtest B: Target closed
    fake_playwright_stub.chromium.raise_on_launch = None
    backend2 = PlaywrightBackend(profile_dir=str(tmp_path / "prof2"))
    backend2.start()

    ctx = fake_playwright_stub.chromium.last_context
    page = ctx.pages[0]
    page.raise_on_goto = Exception("Target page, context or browser has been closed")

    with pytest.raises(BrowserError) as exc_info:
        backend2.goto("https://www.youtube.com")
    assert "ORBIT's browser was closed and will reopen on the next command" in str(exc_info.value)

    # Subtest C: Timeout
    class FakeTimeout(Exception):
        pass

    page.raise_on_goto = FakeTimeout("Navigation timeout of 20000ms exceeded")
    with pytest.raises(BrowserTimeout) as exc_info:
        backend2.goto("https://www.youtube.com")
    assert "timed out" in str(exc_info.value).lower()

    backend2.close()


# --- Test 10: Chromium Sandbox Configuration and Failure Reporting ---

def test_chromium_sandbox_passed_by_default(fake_playwright_stub, tmp_path):
    """launch_persistent_context is invoked with chromium_sandbox=True by default."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    assert ctx.launch_kwargs.get("chromium_sandbox") is True

    backend.close()


def test_chromium_sandbox_disabled_when_setting_off(fake_playwright_stub, tmp_path, monkeypatch):
    """When BROWSER_CHROMIUM_SANDBOX is False, chromium_sandbox=False is passed."""
    monkeypatch.setattr("config.settings.BROWSER_CHROMIUM_SANDBOX", False)
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    assert ctx.launch_kwargs.get("chromium_sandbox") is False

    backend.close()


def test_chromium_sandbox_failure_reports_error_and_setting(fake_playwright_stub, tmp_path):
    """When Chromium fails to launch with the sandbox, an error naming the disabling setting is raised."""
    fake_playwright_stub.chromium.raise_on_launch = Exception("Failed to set up sandbox")
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))

    with pytest.raises(BrowserError) as exc_info:
        backend.start()

    assert "Chromium failed to start with the sandbox enabled" in str(exc_info.value)
    assert "BROWSER_CHROMIUM_SANDBOX=False" in str(exc_info.value)


# --- Test 11: Route Handler Sign-In Abort and Allow Guard ---

def test_route_handler_aborts_signin_urls_unless_allowed(fake_playwright_stub, tmp_path, monkeypatch):
    """Route handler aborts sign-in navigation requests by default, and permits them when BROWSER_ALLOW_SIGNIN=True."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    assert len(ctx.route_handlers) == 1
    pattern, handler = ctx.route_handlers[0]

    signin_url = "https://accounts.google.com/signin/v2"

    # Default: BROWSER_ALLOW_SIGNIN is False -> document navigation route aborted
    monkeypatch.setattr("config.settings.BROWSER_ALLOW_SIGNIN", False)
    route_abort = FakeRoute(signin_url, resource_type="document", is_navigation=True)
    handler(route_abort)
    assert route_abort.aborted is True
    assert route_abort.continued is False

    # Subresource (non-navigation) requests to sign-in URLs are not aborted by the sign-in guard
    route_subres = FakeRoute(signin_url, resource_type="image", is_navigation=False)
    handler(route_subres)
    assert route_subres.continued is True
    assert route_subres.aborted is False

    # Setting on: BROWSER_ALLOW_SIGNIN is True -> navigation route continued
    monkeypatch.setattr("config.settings.BROWSER_ALLOW_SIGNIN", True)
    route_continue = FakeRoute(signin_url, resource_type="document", is_navigation=True)
    handler(route_continue)
    assert route_continue.continued is True
    assert route_continue.aborted is False

    backend.close()


# --- Test 12: Shorts, Ads, and Current Video Exclusions in Tag Script ---

def test_tag_results_script_excludes_shorts_ads_and_current_video():
    """Verify _TAG_RESULTS_SCRIPT contains explicit selectors and filters excluding shorts, ads, and current video."""
    assert "/shorts/" in _TAG_RESULTS_SCRIPT
    assert "promoted" in _TAG_RESULTS_SCRIPT
    assert "ad-" in _TAG_RESULTS_SCRIPT or "ad-slot" in _TAG_RESULTS_SCRIPT
    assert "currentVideoId" in _TAG_RESULTS_SCRIPT
    assert "seen.has(vId)" in _TAG_RESULTS_SCRIPT


# --- Test 13: Click Staying on Same Page Type Reports Failure ---

def test_click_result_stays_on_same_page_type_raises_browser_refused(fake_playwright_stub, tmp_path):
    """click_result raises BrowserRefused if navigation does not change to the expected page type."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    page = ctx.pages[0]
    page.url = "https://www.youtube.com/results?search_query=lofi"

    page.tag_results_override = {
        "count": 1,
        "items": [
            {"index": 1, "title": "Filter Page", "href": "https://www.youtube.com/results?search_query=lofi&sp=m"},
        ],
    }

    with pytest.raises(BrowserRefused, match="Clicking result did not navigate to expected page."):
        backend.click_result(1)

    backend.close()


# --- Test 14: Media Verified Outcome and Autoplay Refusal ---

def test_media_verified_outcome_and_autoplay_refusal(fake_playwright_stub, tmp_path, monkeypatch):
    """media checks real video state, tries 'k' on autoplay block, and returns friendly message without error."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    page = ctx.pages[0]
    page.url = "https://www.youtube.com/watch?v=vid1"
    page.has_video_val = True

    # Play verified
    page.video_state = {"paused": False, "muted": False, "currentTime": 1.0}
    assert backend.media("play") == "Playing."

    # Pause verified
    page.video_state = {"paused": True, "muted": False, "currentTime": 1.0}
    assert backend.media("pause") == "Paused."

    # Autoplay failure fallback without raising exception
    monkeypatch.setattr("time.sleep", lambda s: None)
    page.autoplay_blocked = True
    page.video_state = {"paused": True, "muted": False, "currentTime": 0.0}
    # Video remains paused
    res_autoplay = backend.media("play")
    assert res_autoplay == "The video is open but didn't start; press play."
    assert "k" in page.keyboard.presses

    backend.close()


# --- Test 15: Scroll Bounds and Reporting ---

def test_scroll_already_at_bottom_and_top(fake_playwright_stub, tmp_path):
    """scroll reports 'Already at the bottom.' or 'Already at the top.' when position is unchanged."""
    backend = PlaywrightBackend(profile_dir=str(tmp_path / "prof"))
    backend.start()

    ctx = fake_playwright_stub.chromium.last_context
    page = ctx.pages[0]
    page.url = "https://www.youtube.com/results?search_query=lofi"

    # Already at bottom (max_y style)
    page.scroll_pos = {"y": 1000, "max_y": 1000}
    assert backend.scroll("down", 1.0) == "Already at the bottom."

    # Already at top (max_y style)
    page.scroll_pos = {"y": 0, "max_y": 1000}
    assert backend.scroll("up", 1.0) == "Already at the top."

    # Already at bottom (metrics style: y + innerHeight >= scrollHeight - 2)
    page.scroll_pos = {"y": 1200, "innerHeight": 800, "scrollHeight": 2000}
    assert backend.scroll("down", 1.0) == "Already at the bottom."

    # Already at top (metrics style: y <= 2)
    page.scroll_pos = {"y": 1, "innerHeight": 800, "scrollHeight": 2000}
    assert backend.scroll("up", 1.0) == "Already at the top."

    backend.close()

