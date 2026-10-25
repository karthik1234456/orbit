"""Thread-safe browser controller managing dedicated browser thread and commands."""

import queue
import threading
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote_plus, urlparse

from browser.backend import BrowserBackend
from browser.guards import (
    BrowserError,
    BrowserRefused,
    BrowserTimeout,
    check_url,
    get_site_display_name,
    is_interactive,
    is_signin_url,
    page_type,
)
from config.sites import SITES, resolve_site

SUPPORTED_CLICK_RESULT_PAGE_TYPES = {
    "youtube_home",
    "youtube_results",
    "youtube_watch",
    "youtube_shorts",
    "google_results",
    "wikipedia_results",
    "github_results",
}


def _sanitize_title(text: Any) -> str:
    """Sanitize page title to a single line without angle brackets or backticks, capped at 120 chars."""
    if text is None:
        return ""
    line = " ".join(str(text).strip().split())
    for ch in ("<", ">", "`"):
        line = line.replace(ch, "")
    return line[:120].strip()


def _sanitize_item(text: Any) -> str:
    """Sanitize page text item to a single line without angle brackets or backticks, capped at 100 chars."""
    if text is None:
        return ""
    line = " ".join(str(text).strip().split())
    for ch in ("<", ">", "`"):
        line = line.replace(ch, "")
    return line[:100].strip()


class BrowserController:
    """Controller running all browser automation on one dedicated daemon worker thread.

    Provides synchronous command blocking with configurable timeouts, automatic backend
    restart upon crashes or timeouts, interactive site enforcement, and output sanitization.
    """

    def __init__(
        self,
        backend_factory: Optional[Callable[[], BrowserBackend]] = None,
        timeout_s: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        if backend_factory is None:
            def _default_factory() -> BrowserBackend:
                try:
                    from browser.playwright_backend import PlaywrightBackend
                    return PlaywrightBackend()
                except Exception as exc:
                    raise BrowserError(f"No default browser backend configured: {exc}")
            backend_factory = _default_factory

        self.backend_factory = backend_factory

        if timeout_s is None:
            from config import settings
            self.timeout_s = float(getattr(settings, "BROWSER_COMMAND_TIMEOUT_S", 20))
        else:
            self.timeout_s = float(timeout_s)

        self.clock = clock or time.time
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._backend: Optional[BrowserBackend] = None
        self._suspect: bool = False
        self._shutdown: bool = False
        self._respawn_count: int = 0
        self.last_failure: Optional[dict[str, Any]] = None

    def _ensure_worker(self) -> None:
        """Start or respawn the dedicated daemon thread if not already running."""
        with self._lock:
            if self._shutdown:
                raise BrowserError("BrowserController is shut down.")

            if self._thread is None or not self._thread.is_alive():
                self._respawn_count += 1
                self._thread = threading.Thread(
                    target=self._worker_loop,
                    name="OrbitBrowserThread",
                    daemon=True,
                )
                self._thread.start()

    def _worker_loop(self) -> None:
        """Execution loop running all browser backend calls on the dedicated worker thread."""
        while True:
            try:
                item = self._queue.get()
            except Exception:
                break

            if item is None:  # Poison pill indicating shutdown
                if self._backend is not None:
                    try:
                        self._backend.close()
                    except Exception:
                        pass
                    self._backend = None
                self._queue.task_done()
                break

            cmd_fn, event, result_box = item

            # Check if backend needs initialization or restart
            if self._suspect or self._backend is None or not self._backend.is_alive():
                if self._backend is not None:
                    try:
                        self._backend.close()
                    except Exception:
                        pass
                    self._backend = None

                try:
                    self._backend = self.backend_factory()
                    self._backend.start()
                    self._suspect = False
                except Exception as exc:
                    self._suspect = True
                    result_box[1] = exc
                    event.set()
                    self._queue.task_done()
                    continue

            try:
                res = cmd_fn(self._backend)
                result_box[0] = res
            except Exception as exc:
                result_box[1] = exc

            event.set()
            self._queue.task_done()

    def _dispatch(self, cmd_fn: Callable[[BrowserBackend], Any]) -> Any:
        """Submit a command function to the dedicated worker thread and block for results."""
        self._ensure_worker()

        event = threading.Event()
        result_box = [None, None]  # [result, exception]
        self._queue.put((cmd_fn, event, result_box))

        got_result = event.wait(timeout=self.timeout_s)
        if not got_result:
            self._suspect = True
            raise BrowserTimeout(f"Browser command timed out after {self.timeout_s}s.")

        res, exc = result_box
        if exc is not None:
            if isinstance(exc, (BrowserRefused, BrowserTimeout)):
                raise exc
            self._suspect = True
            raise BrowserError(f"Browser operation failed: {exc}")

        return res

    @staticmethod
    def _ensure_interactive(backend: BrowserBackend) -> None:
        """Verify inside the browser thread that the active page is interactive."""
        current_url = backend.current_url()
        allow_lb = getattr(backend, "allow_loopback", False)
        if not is_interactive(current_url, allow_loopback=allow_lb):
            raise BrowserRefused("I can only control YouTube, Google, Wikipedia and GitHub pages")

    @staticmethod
    def _check_signin_guard(backend: BrowserBackend) -> None:
        """Inspect current URL for authentication or consent walls; revert and refuse if found."""
        curr_url = backend.current_url()
        allow_lb = getattr(backend, "allow_loopback", False)
        if is_signin_url(curr_url, allow_loopback=allow_lb):
            from config import settings
            allow_signin = getattr(settings, "BROWSER_ALLOW_SIGNIN", False)
            if not allow_signin:
                try:
                    backend.history("back")
                except Exception:
                    pass
                site_name = get_site_display_name(curr_url)
                raise BrowserRefused(
                    f"{site_name} asked to sign in, so I stayed where I was. ORBIT's browser is not signed in."
                )

    # --- Public Controller Commands ---

    def navigate(self, url: str) -> dict[str, str]:
        """Navigate to a URL after validating scheme, length, and host safety."""
        def _do(backend: BrowserBackend) -> dict[str, str]:
            allow_lb = getattr(backend, "allow_loopback", False)
            clean_url = check_url(url, allow_loopback=allow_lb)
            backend.goto(clean_url)
            self._check_signin_guard(backend)
            return {
                "url": backend.current_url(),
                "title": backend.title(),
            }

        return self._dispatch(_do)

    def search(self, site: str, query: str) -> dict[str, Any]:
        """Search a configured interactive website using its predefined search template."""
        resolved = resolve_site(site)
        if not resolved or resolved not in SITES:
            raise BrowserRefused(f"Site '{site}' is not configured.")

        cfg = SITES[resolved]
        template = cfg.get("search")
        if not template:
            raise BrowserRefused(f"Site '{site}' does not have a search template configured.")

        if not query or not isinstance(query, str) or not query.strip():
            raise BrowserRefused("Search query cannot be empty.")

        clean_query = query.strip()[:200]
        encoded = quote_plus(clean_query)
        target_url = template.format(query=encoded)
        clean_url = check_url(target_url)

        def _do(backend: BrowserBackend) -> dict[str, Any]:
            backend.goto(clean_url)
            self._check_signin_guard(backend)
            return {
                "site": resolved,
                "query": clean_query,
                "url": backend.current_url(),
                "title": backend.title(),
            }

        return self._dispatch(_do)

    def click_result(self, n: int, next_video: bool = False, for_shorts: bool = False) -> Any:
        """Click the nth search result or interactive item on an interactive page."""
        if not isinstance(n, int) or n < 1 or n > 10:
            raise BrowserRefused(f"Result index must be between 1 and 10, got {n}.")

        def _do(backend: BrowserBackend) -> Any:
            self._ensure_interactive(backend)
            curr = backend.current_url()
            parsed = urlparse(curr)
            host = (parsed.hostname or "").lower()
            if host == "accounts.google.com" or host.endswith(".accounts.google.com"):
                raise BrowserRefused("ORBIT will not click or enter credentials on accounts.google.com.")

            allow_lb = getattr(backend, "allow_loopback", False)
            pt = page_type(curr, allow_loopback=allow_lb)

            if next_video and pt != "youtube_watch":
                raise BrowserRefused("Next video is only available on YouTube watch pages.")

            if pt not in SUPPORTED_CLICK_RESULT_PAGE_TYPES:
                raise BrowserRefused("I can't pick a result on this page. Search for something first.")

            try:
                import inspect
                sig = inspect.signature(backend.click_result)
                kwargs: dict[str, Any] = {}
                if "next_video" in sig.parameters:
                    kwargs["next_video"] = next_video
                if "for_shorts" in sig.parameters:
                    kwargs["for_shorts"] = for_shorts
                res = backend.click_result(n, **kwargs)
                self._check_signin_guard(backend)
                return res
            except Exception as exc:
                if hasattr(exc, "diag") and exc.diag:
                    self.last_failure = exc.diag
                elif getattr(backend, "last_failure", None):
                    self.last_failure = backend.last_failure
                raise

        return self._dispatch(_do)

    def media(self, action: str) -> Any:
        """Control media playback on an interactive page."""
        if not action or not isinstance(action, str):
            raise BrowserRefused("Media action must be a non-empty string.")

        act = action.strip().lower()
        if act not in ("play", "pause", "toggle", "mute", "unmute"):
            raise BrowserRefused(
                f"Invalid media action '{action}'. Must be one of: play, pause, toggle, mute, unmute."
            )

        def _do(backend: BrowserBackend) -> Any:
            self._ensure_interactive(backend)
            allow_lb = getattr(backend, "allow_loopback", False)
            curr = backend.current_url()
            pt = page_type(curr, allow_loopback=allow_lb)
            has_vid_fn = getattr(backend, "has_video", None)
            video_present = has_vid_fn() if callable(has_vid_fn) else False
            if pt != "youtube_watch" and not video_present:
                raise BrowserRefused("There is no video on this page to control.")
            return backend.media(act)

        return self._dispatch(_do)

    def scroll(self, direction: str, screens: float = 1.0) -> Any:
        """Scroll the viewport up, down, top, or bottom on an interactive page."""
        if not direction or not isinstance(direction, str):
            raise BrowserRefused("Scroll direction must be a non-empty string.")

        dir_clean = direction.strip().lower()
        if dir_clean not in ("down", "up", "top", "bottom"):
            raise BrowserRefused(
                f"Invalid scroll direction '{direction}'. Must be one of: down, up, top, bottom."
            )

        try:
            sc = float(screens)
        except (ValueError, TypeError):
            raise BrowserRefused(f"Screens must be a number between 1 and 5, got {screens}.")

        if sc < 1.0 or sc > 5.0:
            raise BrowserRefused(f"Screens must be between 1 and 5, got {screens}.")

        def _do(backend: BrowserBackend) -> Any:
            self._ensure_interactive(backend)
            return backend.scroll(dir_clean, sc)

        return self._dispatch(_do)

    def history(self, action: str) -> Any:
        """Navigate page history (back, forward, reload) on an interactive page."""
        if not action or not isinstance(action, str):
            raise BrowserRefused("History action must be a non-empty string.")

        act = action.strip().lower()
        if act not in ("back", "forward", "reload"):
            raise BrowserRefused(
                f"Invalid history action '{action}'. Must be one of: back, forward, reload."
            )

        def _do(backend: BrowserBackend) -> Any:
            self._ensure_interactive(backend)
            res = backend.history(act)
            self._check_signin_guard(backend)
            return res

        return self._dispatch(_do)

    def key(self, name: str) -> str:
        """Send a keyboard key press on an interactive page."""
        from config.browser_sites import ALLOWED_BROWSER_KEYS
        if not name or not isinstance(name, str) or name not in ALLOWED_BROWSER_KEYS:
            raise BrowserRefused(
                f"Key '{name}' is not allowed. Permitted keys: {sorted(ALLOWED_BROWSER_KEYS)}"
            )

        def _do(backend: BrowserBackend) -> str:
            self._ensure_interactive(backend)
            return backend.key(name)

        return self._dispatch(_do)

    def read_page(self) -> dict[str, Any]:
        """Read and sanitize current page contents from an interactive page."""
        def _do(backend: BrowserBackend) -> dict[str, Any]:
            self._ensure_interactive(backend)
            return backend.read_page()

        raw = self._dispatch(_do)

        raw_title = raw.get("title", "")
        clean_title = _sanitize_title(raw_title)

        raw_items = raw.get("items", [])
        clean_items: list[str] = []
        for item in raw_items:
            s = _sanitize_item(item)
            if s:
                clean_items.append(s)
            if len(clean_items) == 10:
                break

        return {
            "title": clean_title,
            "url": str(raw.get("url", "")),
            "items": clean_items,
        }

    def close(self) -> None:
        """Close the active browser instance."""
        with self._lock:
            if self._backend is None or not self._backend.is_alive():
                return

        def _do(backend: BrowserBackend) -> None:
            backend.close()

        self._dispatch(_do)

    def status(self) -> dict[str, Any]:
        """Return the current operational status of the browser."""
        from config import settings
        sandbox_enabled = bool(getattr(settings, "BROWSER_CHROMIUM_SANDBOX", True))

        with self._lock:
            if self._shutdown:
                raise BrowserError("BrowserController is shut down.")
            if not self._suspect and (self._backend is None or not self._backend.is_alive()):
                res = {
                    "alive": False,
                    "url": "",
                    "title": "",
                }
                if not sandbox_enabled:
                    res["warning"] = "Chromium sandbox is disabled (BROWSER_CHROMIUM_SANDBOX=False)."
                return res

        def _do(backend: BrowserBackend) -> dict[str, Any]:
            st = {
                "alive": backend.is_alive(),
                "url": backend.current_url(),
                "title": backend.title(),
            }
            if not sandbox_enabled:
                st["warning"] = "Chromium sandbox is disabled (BROWSER_CHROMIUM_SANDBOX=False)."
            return st

        return self._dispatch(_do)

    def is_alive(self) -> bool:
        """Return True if the controller worker thread is active and running."""
        with self._lock:
            return not self._shutdown and self._thread is not None and self._thread.is_alive()

    def shutdown(self) -> None:
        """Shut down the controller and worker thread idempotently."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._queue.put(None)
            thread = self._thread

        if thread and thread.is_alive():
            thread.join(timeout=1.0)
