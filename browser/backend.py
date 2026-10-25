"""Abstract browser backend interface and scriptable test backend."""

from abc import ABC, abstractmethod
import threading
import time
from typing import Any, Mapping, Optional


class BrowserBackend(ABC):
    """Abstract interface defining required browser automation operations."""

    @abstractmethod
    def start(self) -> None:
        """Initialize and launch the underlying browser engine."""
        pass

    @abstractmethod
    def goto(self, url: str) -> None:
        """Navigate the active tab to the specified URL."""
        pass

    @abstractmethod
    def current_url(self) -> str:
        """Return the current URL of the active browser page."""
        pass

    @abstractmethod
    def title(self) -> str:
        """Return the page title of the active browser page."""
        pass

    @abstractmethod
    def click_result(self, n: int) -> Any:
        """Click the nth search result or interactive item on the page (1-indexed)."""
        pass

    @abstractmethod
    def media(self, action: str) -> Any:
        """Execute a media playback control action (play, pause, toggle, mute, unmute)."""
        pass

    @abstractmethod
    def scroll(self, direction: str, screens: float) -> None:
        """Scroll the active viewport (down, up, top, bottom) by the specified screens."""
        pass

    @abstractmethod
    def history(self, action: str) -> None:
        """Navigate page history (back, forward, reload)."""
        pass

    @abstractmethod
    def key(self, name: str) -> str:
        """Send a keyboard key press (e.g. ArrowDown, ArrowUp, k, space)."""
        pass

    @abstractmethod
    def read_page(self) -> dict[str, Any]:
        """Extract title, current URL, and high-level readable items/links from the page."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Shut down the browser instance and release all associated resources."""
        pass

    @abstractmethod
    def is_alive(self) -> bool:
        """Return True if the browser backend is initialized and responsive."""
        pass


class FakeBackend(BrowserBackend):
    """Scriptable test backend that records every call and thread identity without external browsers."""

    def __init__(
        self,
        pages: Optional[Mapping[str, Mapping[str, Any]]] = None,
        sleep_on: Optional[Mapping[str, float]] = None,
        crash_on: Optional[Mapping[str, Exception]] = None,
    ):
        self.pages = dict(pages) if pages else {}
        self.sleep_on = dict(sleep_on) if sleep_on else {}
        self.crash_on = dict(crash_on) if crash_on else {}

        self.calls: list[dict[str, Any]] = []
        self._alive: bool = False
        self._current_url: str = "about:blank"
        self._title: str = ""
        self._items: list[Any] = []
        self._history: list[str] = []
        self._history_idx: int = -1
        self._has_video: bool = False
        self._video_state: dict[str, Any] = {
            "paused": True,
            "muted": False,
            "currentTime": 0.0,
        }
        self._autoplay_blocked: bool = False
        self._autoplay_attempts: int = 0
        self._scroll_y: int = 0
        self._max_scroll_y: int = 1000
        self.last_failure: Optional[dict[str, Any]] = None
        self.google_blocked_until: float = 0.0

    def start_google_cooldown(self, duration_minutes: Optional[int] = None) -> None:
        from config import settings
        mins = duration_minutes if duration_minutes is not None else getattr(settings, "BROWSER_BLOCK_COOLDOWN_MINUTES", 30)
        self.google_blocked_until = time.time() + (mins * 60)

    def is_google_blocked(self) -> bool:
        return time.time() < self.google_blocked_until

    def _check_verification_page(self) -> None:
        from browser.guards import BrowserRefused, is_blocked_url
        curr_url = self.current_url() if hasattr(self, "current_url") else self._current_url
        allow_lb = getattr(self, "allow_loopback", False)
        is_blocked = is_blocked_url(curr_url, allow_loopback=allow_lb)

        if not is_blocked:
            matched_page = None
            for p_url, p_data in self.pages.items():
                if curr_url == p_url or curr_url.startswith(p_url) or p_url.startswith(curr_url):
                    matched_page = p_data
                    break
            text_sample = ""
            if matched_page:
                text_sample = str(matched_page.get("text", matched_page.get("body", "")))[:300]
            if "unusual traffic" in text_sample.lower():
                is_blocked = True

        if is_blocked:
            self.start_google_cooldown()
            try:
                self.history("back")
            except Exception:
                pass
            raise BrowserRefused(
                "Google showed a verification page for automated browsing, so I stopped. I will not try to get around it."
            )


    def key(self, name: str) -> str:
        self._record("key", name=name)
        return f"Pressed {name}."

    def _record(self, method: str, **kwargs: Any) -> None:
        """Record method invocation along with thread metadata and simulate sleep/crash."""
        ident = threading.get_ident()
        name = threading.current_thread().name
        self.calls.append({
            "method": method,
            "args": kwargs,
            "thread_id": ident,
            "thread_name": name,
        })

        if method in self.sleep_on:
            time.sleep(self.sleep_on[method])

        if method in self.crash_on:
            exc = self.crash_on.pop(method)
            self._alive = False
            raise exc

    def start(self) -> None:
        self._record("start")
        self._alive = True

    def goto(self, url: str, record_history: bool = True) -> None:
        self._record("goto", url=url)
        self._current_url = url
        if record_history:
            self._history = self._history[: self._history_idx + 1] + [url]
            self._history_idx = len(self._history) - 1

        matched_page = None
        for p_url, p_data in self.pages.items():
            if url == p_url or url.startswith(p_url) or p_url.startswith(url):
                matched_page = p_data
                break

        from browser.guards import page_type

        pt = page_type(url)
        if matched_page:
            self._title = str(matched_page.get("title", ""))
            raw_items = matched_page.get("links", matched_page.get("items", []))
            self._items = list(raw_items)
            self._has_video = bool(matched_page.get("video", pt == "youtube_watch"))
            if "scroll_y" in matched_page:
                self._scroll_y = int(matched_page["scroll_y"])
            if "max_scroll_y" in matched_page:
                self._max_scroll_y = int(matched_page["max_scroll_y"])
        else:
            self._title = f"Title for {url}"
            if pt in ("youtube_results", "youtube_home", "youtube_watch", "google_results", "wikipedia_results", "github_results"):
                self._items = [f"Result {i}" for i in range(1, 11)]
            else:
                self._items = []
            self._has_video = (pt in ("youtube_watch", "youtube_home"))

        self._check_verification_page()


    def current_url(self) -> str:
        self._record("current_url")
        return self._current_url

    def title(self) -> str:
        self._record("title")
        return self._title

    def has_video(self) -> bool:
        from browser.guards import page_type
        return self._has_video or page_type(self._current_url) in ("youtube_watch", "youtube_home")

    def click_result(self, n: int, next_video: bool = False, for_shorts: bool = False) -> str:
        self._record("click_result", n=n, next_video=next_video, for_shorts=for_shorts)
        from browser.guards import BrowserRefused, page_type
        from urllib.parse import urlparse

        # If items appear after navigation (e.g. 1 second delay), poll up to 4.0s
        start_wait = time.time()
        while (time.time() - start_wait) < 4.0:
            if callable(getattr(self, "_items_provider", None)):
                self._items = list(self._items_provider())
            elif hasattr(self, "_items_appear_at") and time.time() >= self._items_appear_at:
                if hasattr(self, "_delayed_items"):
                    self._items = list(self._delayed_items)
            if len(self._items) > 0:
                break
            time.sleep(0.05)

        if for_shorts:
            # Filter for shorts items only
            filtered = []
            for it in self._items:
                href = it.get("url") or it.get("href") if isinstance(it, dict) else (it if isinstance(it, str) else "")
                if "/shorts/" in href:
                    filtered.append(it)
            self._items = filtered
        else:
            # Normal clicks: exclude any /shorts/ items
            filtered = []
            for it in self._items:
                href = it.get("url") or it.get("href") if isinstance(it, dict) else (it if isinstance(it, str) else "")
                if "/shorts/" in href:
                    continue
                filtered.append(it)
            self._items = filtered

        if len(self._items) == 0:
            allow_lb = getattr(self, "allow_loopback", False)
            pt = page_type(self._current_url, allow_loopback=allow_lb)
            parsed = urlparse(self._current_url)
            diag = {
                "page_type": pt,
                "url_host": parsed.netloc or parsed.hostname or "",
                "url_path": parsed.path or "",
                "anchors_per_selector": getattr(self, "_anchors_per_selector", {}),
                "exclusions": getattr(self, "_exclusions", {}),
                "viewport_size": {"width": 1280, "height": 720},
            }
            self.last_failure = diag
            exc = BrowserRefused(f"No videos found on this page ({pt}). Try scrolling down or searching first.")
            exc.diag = diag
            raise exc

        if n > len(self._items) or n < 1:
            raise BrowserRefused(f"I only found {len(self._items)} results on this page.")

        item = self._items[n - 1]
        target_url = None
        title_str = ""

        if isinstance(item, dict):
            target_url = item.get("url") or item.get("href")
            title_str = item.get("title") or item.get("text") or ""
        elif isinstance(item, str):
            if item.startswith("http://") or item.startswith("https://") or item.startswith("/"):
                target_url = item
                title_str = item
            else:
                title_str = item

        url_before = self._current_url
        pt_before = page_type(url_before)
        if not target_url:
            if pt_before.startswith("youtube_"):
                if pt_before == "youtube_watch":
                    target_url = f"https://www.youtube.com/watch?v=item_{n}_next"
                else:
                    target_url = f"https://www.youtube.com/watch?v=item_{n}"
            elif pt_before.startswith("google_"):
                target_url = f"https://example.com/result_{n}"
            elif pt_before.startswith("wikipedia_"):
                target_url = f"https://en.wikipedia.org/wiki/Result_{n}"
            elif pt_before.startswith("github_"):
                target_url = f"https://github.com/orbit/result_{n}"
            else:
                target_url = f"https://www.youtube.com/watch?v=item_{n}"

        if not title_str:
            title_str = f"Result #{n}"

        if target_url:
            if target_url.startswith("/"):
                from urllib.parse import urljoin
                target_url = urljoin(url_before, target_url)
            self.goto(target_url)

        url_after = self._current_url
        from browser.guards import verify_click_result
        valid, reason = verify_click_result(url_before, url_after)
        if not valid:
            if reason == "signin":
                self.history("back")
                raise BrowserRefused(
                    "Google asked to sign in, so I stayed where I was. ORBIT's browser is not signed in."
                )
            raise BrowserRefused("Clicking result did not navigate to expected page.")

        # Sanitize title to 100 chars
        clean_title = " ".join(str(title_str).strip().split())
        for ch in ("<", ">", "`"):
            clean_title = clean_title.replace(ch, "")
        clean_title = clean_title[:100].strip()

        return f"Opened result {n}: {clean_title}"

    def media(self, action: str) -> str:
        self._record("media", action=action)
        from browser.guards import BrowserRefused

        act = action.strip().lower()
        if not self.has_video():
            raise BrowserRefused("There is no video on this page to control.")

        if act == "play":
            if self._autoplay_blocked:
                self._autoplay_attempts += 1
                if self._autoplay_attempts == 1:
                    # Video remains paused due to autoplay policy
                    self._video_state["paused"] = True
                    return "The video is open but didn't start; press play."
                else:
                    self._video_state["paused"] = False
                    return "Playing."
            self._video_state["paused"] = False
            return "Playing."
        elif act == "pause":
            self._video_state["paused"] = True
            return "Paused."
        elif act == "toggle":
            if self._video_state.get("paused", True):
                if self._autoplay_blocked:
                    self._autoplay_attempts += 1
                    return "The video is open but didn't start; press play."
                self._video_state["paused"] = False
                return "Playing."
            else:
                self._video_state["paused"] = True
                return "Paused."
        elif act == "mute":
            self._video_state["muted"] = True
            return "Muted."
        elif act == "unmute":
            self._video_state["muted"] = False
            return "Unmuted."

        return f"Media action '{act}' executed."

    def scroll(self, direction: str, screens: float) -> str:
        self._record("scroll", direction=direction, screens=screens)
        from browser.guards import BrowserRefused

        d = direction.strip().lower()
        delta = int(screens * 600)

        if d == "down":
            if self._scroll_y >= self._max_scroll_y:
                return "Already at the bottom."
            new_y = min(self._max_scroll_y, self._scroll_y + delta)
            diff = new_y - self._scroll_y
            self._scroll_y = new_y
            if diff <= 0:
                return "Already at the bottom."
            return f"Scrolled down {diff} px."

        elif d == "bottom":
            if self._scroll_y >= self._max_scroll_y:
                return "Already at the bottom."
            diff = self._max_scroll_y - self._scroll_y
            self._scroll_y = self._max_scroll_y
            return f"Scrolled down {diff} px."

        elif d == "up":
            if self._scroll_y <= 0:
                return "Already at the top."
            new_y = max(0, self._scroll_y - delta)
            diff = self._scroll_y - new_y
            self._scroll_y = new_y
            if diff <= 0:
                return "Already at the top."
            return f"Scrolled up {diff} px."

        elif d == "top":
            if self._scroll_y <= 0:
                return "Already at the top."
            diff = self._scroll_y
            self._scroll_y = 0
            return f"Scrolled up {diff} px."

        raise BrowserRefused(f"Invalid scroll direction '{direction}'.")

    def history(self, action: str) -> str:
        self._record("history", action=action)
        from browser.guards import BrowserRefused, page_type

        act = action.strip().lower()
        if act == "back":
            if self._history_idx > 0:
                self._history_idx -= 1
                prev_url = self._history[self._history_idx]
                self.goto(prev_url, record_history=False)
                return page_type(self._current_url)
            return "No earlier page."
        elif act == "forward":
            if self._history_idx < len(self._history) - 1:
                self._history_idx += 1
                next_url = self._history[self._history_idx]
                self.goto(next_url, record_history=False)
                return page_type(self._current_url)
            return "No earlier page."
        elif act == "reload":
            self._check_verification_page()
            return page_type(self._current_url)

        raise BrowserRefused(f"Invalid history action '{action}'.")

    def read_page(self) -> dict[str, Any]:
        self._record("read_page")
        return {
            "title": self._title,
            "url": self._current_url,
            "items": list(self._items),
        }

    def close(self) -> None:
        self._record("close")
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive
