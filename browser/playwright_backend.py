"""Playwright-backed browser automation backend for ORBIT.

Provides managed Chromium/Chrome browser automation using Playwright's synchronous API.
Enforces security guards: URL validation, network request interception (blocking private
and loopback targets), automatic dialog dismissal, download blocking, strict page capping,
and execution of fixed module-level JavaScript scripts only.

API Signature Notes:
Playwright is not bundled by default. When installed via `requirements-browser.txt`,
this backend follows the documented sync API of Playwright:
- `playwright.sync_api.sync_playwright()`: Context manager or factory returning Playwright.
- `Chromium.launch_persistent_context(user_data_dir, channel, headless, accept_downloads, chromium_sandbox)`:
  Launches persistent browser context preserving cookies/state in the profile folder.
- `BrowserContext.route(url_pattern, handler)`: Intercepts network requests.
- `Page.on("dialog", handler)`: Listens for alert/confirm/prompt dialogs and dismisses them.
- `BrowserContext.on("page", handler)`: Listens for popups and newly opened tabs.
- `Page.goto(url, wait_until, timeout)`: Navigates active page.
- `Page.locator(selector)`: Locates elements matching CSS selector.
- `Locator.count()`, `Locator.nth(i)`, `Locator.is_visible()`, `Locator.get_attribute(name)`:
  Element inspection and visibility checks.
- `Locator.click()`: Performs element click.
- `Page.wait_for_selector(selector, timeout)`: Waits for element attached/visible.
- `Page.evaluate(script)`: Executes fixed JavaScript string on page.
- `Page.mouse.move(x, y)`, `Page.mouse.wheel(delta_x, delta_y)`: Viewport scrolling.
- `Page.keyboard.press(key)`: Keyboard navigation (Home/End, k for play).
- `Page.go_back()`, `Page.go_forward()`, `Page.reload()`: History navigation.
"""
import asyncio
import inspect
from pathlib import Path
import time
from typing import Any, Optional
from urllib.parse import urlparse

from browser.backend import BrowserBackend
from browser.guards import (
    BrowserError,
    BrowserRefused,
    BrowserTimeout,
    check_url,
    is_signin_url,
    page_type,
    request_allowed,
    verify_click_result,
)
from config import settings

# --- Fixed JavaScript Module Constants ---
# To prevent code injection, NO tool parameter may ever reach page.evaluate,
# a selector, or a script. All browser evaluation runs these immutable constants.

_MEDIA_PLAY_SCRIPT = """() => {
    const v = document.querySelector('video');
    if (v) {
        v.play();
        return {status: 'ok', action: 'play'};
    }
    return {status: 'no_video'};
}"""

_MEDIA_PAUSE_SCRIPT = """() => {
    const v = document.querySelector('video');
    if (v) {
        v.pause();
        return {status: 'ok', action: 'pause'};
    }
    return {status: 'no_video'};
}"""

_MEDIA_TOGGLE_SCRIPT = """() => {
    const v = document.querySelector('video');
    if (v) {
        if (v.paused) {
            v.play();
        } else {
            v.pause();
        }
        return {status: 'ok', action: 'toggle', paused: v.paused};
    }
    return {status: 'no_video'};
}"""

_MEDIA_MUTE_SCRIPT = """() => {
    const v = document.querySelector('video');
    if (v) {
        v.muted = true;
        return {status: 'ok', action: 'mute'};
    }
    return {status: 'no_video'};
}"""

_MEDIA_UNMUTE_SCRIPT = """() => {
    const v = document.querySelector('video');
    if (v) {
        v.muted = false;
        return {status: 'ok', action: 'unmute'};
    }
    return {status: 'no_video'};
}"""

_MEDIA_SCRIPTS: dict[str, str] = {
    "play": _MEDIA_PLAY_SCRIPT,
    "pause": _MEDIA_PAUSE_SCRIPT,
    "toggle": _MEDIA_TOGGLE_SCRIPT,
    "mute": _MEDIA_MUTE_SCRIPT,
    "unmute": _MEDIA_UNMUTE_SCRIPT,
}

_HAS_VIDEO_SCRIPT = "() => Boolean(document.querySelector('video'))"

_GET_VIDEO_STATE_SCRIPT = """() => {
    const v = document.querySelector('video');
    if (!v) return null;
    return {
        paused: Boolean(v.paused),
        muted: Boolean(v.muted),
        currentTime: Number(v.currentTime) || 0
    };
}"""

_GET_SCROLL_POS_SCRIPT = """() => {
    const el = document.scrollingElement || document.documentElement || document.body || {};
    return {
        y: Math.round(window.scrollY || window.pageYOffset || el.scrollTop || 0),
        innerHeight: Math.round(window.innerHeight || 0),
        scrollHeight: Math.round(el.scrollHeight || 0)
    };
}"""

_SCROLL_BY_SCRIPT = "(deltaY) => { window.scrollBy(0, deltaY); }"

_TAG_RESULTS_SCRIPT = """(forShorts) => {
    const prev = document.querySelectorAll('[data-orbit-result]');
    for (const el of prev) {
        el.removeAttribute('data-orbit-result');
    }

    const url = window.location.href || '';
    let parsedUrl;
    try {
        parsedUrl = new URL(url);
    } catch (e) {
        return { count: 0, items: [], diag: {} };
    }
    const host = parsedUrl.hostname.toLowerCase();
    const path = parsedUrl.pathname;

    const results = [];
    const seen = new Set();
    const anchorsPerSelector = {};
    const exclusions = {
        navigation_guide: 0,
        ads_promoted: 0,
        shorts_shelves: 0,
        mix_playlist: 0,
        current_video: 0,
        hidden_or_zero_size: 0,
        shorts_excluded: 0,
    };

    function isVisible(el) {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
            return false;
        }
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    if (host.includes('youtube.com') || host.includes('youtu.be') || host === '127.0.0.1' || host === 'localhost') {
        let container = null;
        let currentVideoId = null;

        if (path.startsWith('/watch')) {
            container = document.querySelector('#secondary, #related, ytd-watch-next-secondary-results-renderer');
            currentVideoId = parsedUrl.searchParams.get('v');
        } else if (path.startsWith('/results')) {
            container = document.querySelector('ytd-search #contents, ytd-section-list-renderer, #contents');
        } else if (path.startsWith('/shorts')) {
            container = document.querySelector('#contents, ytd-shorts, #shorts-container');
        } else {
            container = document.querySelector('ytd-browse #contents, ytd-rich-grid-renderer, #contents');
        }

        // If container selector matches nothing, fall back to whole document
        if (!container || !container.querySelector('a[href]')) {
            container = document.body || document.documentElement || document;
        }

        function checkExclusion(a, aUrl) {
            if (a.closest('#guide, ytd-guide-renderer, ytd-mini-guide-renderer, nav, #masthead, ytd-masthead')) {
                exclusions.navigation_guide++;
                return true;
            }
            if (a.closest('ytd-promoted-video-renderer, ytd-ad-slot-renderer, [class*="ad-"], [class*="promoted"]')) {
                exclusions.ads_promoted++;
                return true;
            }
            if (a.closest('ytd-reel-shelf-renderer, ytd-shorts, [is-shorts]')) {
                if (!forShorts) {
                    exclusions.shorts_shelves++;
                    return true;
                }
            }
            if (a.closest('ytd-radio-renderer, ytd-playlist-renderer, ytd-mix-renderer')) {
                exclusions.mix_playlist++;
                return true;
            }
            if (aUrl) {
                if (aUrl.searchParams.get('start_radio') === '1' || (aUrl.searchParams.get('list') || '').startsWith('RD') || aUrl.pathname.startsWith('/playlist')) {
                    exclusions.mix_playlist++;
                    return true;
                }
                if (forShorts) {
                    if (!aUrl.pathname.startsWith('/shorts/')) {
                        return true;
                    }
                } else {
                    if (aUrl.pathname.startsWith('/shorts/')) {
                        exclusions.shorts_excluded++;
                        return true;
                    }
                }
                const vId = aUrl.searchParams.get('v');
                if (currentVideoId && vId && vId === currentVideoId) {
                    exclusions.current_video++;
                    return true;
                }
            }
            if (!isVisible(a)) {
                exclusions.hidden_or_zero_size++;
                return true;
            }
            return false;
        }

        function extractTitle(a) {
            const card = a.closest('ytd-video-renderer, ytd-rich-item-renderer, ytd-compact-video-renderer, .video-card, .video-item, .rec-item, .shorts-item') || a;
            const titleEl = card.querySelector('#video-title, #video-title-link, a[title]') || a;
            let rawTitle = titleEl.getAttribute('title') || titleEl.getAttribute('aria-label') || a.getAttribute('title') || a.getAttribute('aria-label') || '';
            if (!rawTitle) {
                rawTitle = a.innerText || a.textContent || '';
                rawTitle = rawTitle.replace(/\\b\\d{1,2}:\\d{2}(:\\d{2})?\\b/g, '');
                rawTitle = rawTitle.replace(/\\b(now playing|live)\\b/gi, '');
            }
            let title = rawTitle.replace(/\\s+/g, ' ').replace(/[<>`]/g, '').trim();
            return title.slice(0, 100).trim();
        }

        if (forShorts) {
            // (c) for Shorts pages: /shorts/ anchors ONLY when user asks for shorts
            const shortsSelectors = ["a[href*='/shorts/']"];
            for (const sel of shortsSelectors) {
                const els = container.querySelectorAll(sel);
                anchorsPerSelector[sel] = els.length;
                for (const a of els) {
                    if (results.length >= 10) break;
                    let aUrl;
                    try { aUrl = new URL(a.href, window.location.href); } catch (e) { continue; }
                    if (checkExclusion(a, aUrl)) continue;
                    const key = aUrl.pathname;
                    if (seen.has(key)) continue;
                    const title = extractTitle(a) || 'Short Video';
                    seen.add(key);
                    results.push({ el: a, title: title, href: a.href });
                }
            }
        } else {
            // (a) anchors whose href path is /watch with a v= id
            const watchSelectors = ["a[href*='/watch?v=']", "a[href*='/watch/']"];
            for (const sel of watchSelectors) {
                const els = container.querySelectorAll(sel);
                anchorsPerSelector[sel] = els.length;
                for (const a of els) {
                    if (results.length >= 10) break;
                    let aUrl;
                    try { aUrl = new URL(a.href, window.location.href); } catch (e) { continue; }
                    if (checkExclusion(a, aUrl)) continue;
                    const vId = aUrl.searchParams.get('v');
                    if (!vId) continue;
                    if (seen.has(vId)) continue;
                    const title = extractTitle(a);
                    if (!title) continue;
                    seen.add(vId);
                    results.push({ el: a, title: title, href: a.href });
                }
            }

            // (b) anchors in video renderer elements
            if (results.length < 10) {
                const rendererSelectors = [
                    "ytd-video-renderer a#video-title",
                    "ytd-video-renderer a#video-title-link",
                    "ytd-rich-item-renderer a#video-title-link",
                    "a#video-title",
                    "ytd-video-renderer a.ytd-video-renderer",
                    "ytd-compact-video-renderer a#video-title",
                    ".video-card a",
                    ".video-item a",
                    ".rec-item a"
                ];
                for (const sel of rendererSelectors) {
                    const els = container.querySelectorAll(sel);
                    anchorsPerSelector[sel] = els.length;
                    for (const a of els) {
                        if (results.length >= 10) break;
                        let aUrl;
                        try { aUrl = new URL(a.href, window.location.href); } catch (e) { continue; }
                        if (checkExclusion(a, aUrl)) continue;
                        const vId = aUrl.searchParams.get('v') || aUrl.href;
                        if (seen.has(vId)) continue;
                        const title = extractTitle(a);
                        if (!title) continue;
                        seen.add(vId);
                        results.push({ el: a, title: title, href: a.href });
                    }
                }
            }
        }

    } else if (host === 'google.com' || host.endsWith('.google.com') || /^(www\\.)?google\\.(com|co\\.[a-z]{2}|com\\.[a-z]{2}|[a-z]{2})$/i.test(host)) {
        const anchors = document.querySelectorAll('#search a[href], #rso a[href], .g a[href]');
        for (const a of anchors) {
            if (!a.querySelector('h1, h2, h3, h4')) {
                continue;
            }
            let aUrl;
            try {
                aUrl = new URL(a.href, window.location.href);
            } catch (e) {
                continue;
            }
            if (!aUrl.protocol.startsWith('http')) {
                continue;
            }
            const aHost = aUrl.hostname.toLowerCase();
            if (aHost === 'google.com' || aHost.endsWith('.google.com') || /^(www\\.)?google\\.(com|co\\.[a-z]{2}|com\\.[a-z]{2}|[a-z]{2})$/i.test(aHost)) {
                continue;
            }
            if (seen.has(a.href)) {
                continue;
            }
            if (!isVisible(a)) {
                continue;
            }
            let title = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
            if (!title) continue;

            seen.add(a.href);
            results.push({ el: a, title: title, href: a.href });
            if (results.length >= 10) break;
        }

    } else if (host.includes('wikipedia.org')) {
        const anchors = document.querySelectorAll('.mw-search-result-heading a, .mw-search-results a, #mw-content-text .mw-search-result-title a, ul.mw-search-results li a');
        for (const a of anchors) {
            if (!isVisible(a)) continue;
            let aUrl;
            try {
                aUrl = new URL(a.href, window.location.href);
            } catch (e) {
                continue;
            }
            if (seen.has(aUrl.href)) continue;
            let title = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
            if (!title) continue;
            seen.add(aUrl.href);
            results.push({ el: a, title: title, href: aUrl.href });
            if (results.length >= 10) break;
        }

    } else if (host.includes('github.com')) {
        const anchors = document.querySelectorAll('div[data-testid="results-list"] a[href], .repo-list-item a, .search-title a, a.v-align-middle');
        for (const a of anchors) {
            if (!isVisible(a)) continue;
            let aUrl;
            try {
                aUrl = new URL(a.href, window.location.href);
            } catch (e) {
                continue;
            }
            if (seen.has(aUrl.href)) continue;
            let title = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
            if (!title) continue;
            seen.add(aUrl.href);
            results.push({ el: a, title: title, href: aUrl.href });
            if (results.length >= 10) break;
        }
    }

    const out = [];
    for (let i = 0; i < results.length; i++) {
        results[i].el.setAttribute('data-orbit-result', String(i + 1));
        out.push({
            index: i + 1,
            title: results[i].title,
            href: results[i].href
        });
    }

    return {
        count: out.length,
        items: out,
        diag: {
            url_host: host,
            url_path: path,
            anchors_per_selector: anchorsPerSelector,
            exclusions: exclusions,
            viewport_size: { width: window.innerWidth, height: window.innerHeight }
        }
    };
}"""

_READ_PAGE_SCRIPT = """() => {
    const url = window.location.href || '';
    let parsedUrl;
    try {
        parsedUrl = new URL(url);
    } catch (e) {
        return { title: document.title || '', url: url, items: [] };
    }
    const host = parsedUrl.hostname.toLowerCase();
    const path = parsedUrl.pathname;
    const isWatch = (host.includes('youtube.com') || host.includes('youtu.be') || host === '127.0.0.1' || host === 'localhost') && path.startsWith('/watch');

    const results = [];
    const seen = new Set();
    const skippedHeadings = new Set([
        'comments', 'comment', 'products', 'product', 'live chat replay',
        'live chat', 'chat replay', 'transcript', 'chapters', 'up next',
        'up next / recommended', 'recommended'
    ]);

    function cleanText(raw) {
        if (!raw) return '';
        let s = raw.replace(/\\b\\d{1,2}:\\d{2}(:\\d{2})?\\b/g, '');
        s = s.replace(/\\b(now playing|live)\\b/gi, '');
        s = s.replace(/[\\n\\r\\t]+/g, ' ').replace(/\\s+/g, ' ');
        s = s.replace(/[<>`]/g, '').trim();
        return s.slice(0, 100).trim();
    }

    if (isWatch) {
        const mainTitleEl = document.querySelector('h1.ytd-watch-metadata, #title h1, ytd-watch-metadata #title, #primary h1, #primary h2, .primary h2, h1');
        if (mainTitleEl) {
            const mainText = cleanText(mainTitleEl.getAttribute('title') || mainTitleEl.getAttribute('aria-label') || mainTitleEl.innerText || mainTitleEl.textContent);
            if (mainText && !skippedHeadings.has(mainText.toLowerCase())) {
                seen.add(mainText);
                results.push(mainText);
            }
        }
        const recEls = document.querySelectorAll('#secondary #video-title, #related #video-title, ytd-compact-video-renderer a#video-title, #secondary .rec-item a, #secondary a[href*="/watch"]');
        for (const el of recEls) {
            if (results.length >= 10) break;
            if (el.closest('ytd-promoted-video-renderer, ytd-ad-slot-renderer, [class*="ad-"], [class*="promoted"], ytd-reel-shelf-renderer')) {
                continue;
            }
            if (el.href && (el.href.includes('start_radio=1') || el.href.includes('list=RD') || el.href.includes('/playlist') || el.href.includes('/shorts/'))) {
                continue;
            }
            const txt = cleanText(el.getAttribute('title') || el.getAttribute('aria-label') || el.innerText || el.textContent);
            if (txt && !seen.has(txt) && !skippedHeadings.has(txt.toLowerCase())) {
                seen.add(txt);
                results.push(txt);
            }
        }
    } else {
        const selectors = [
            '#contents ytd-video-renderer #video-title',
            'ytd-video-renderer h3',
            '#search .g h3',
            '#rso .g h3',
            '.mw-search-result-heading a',
            '.repo-list-item a',
            'main h1, main h2, main h3',
            'h1, h2, h3',
            'a[href]'
        ];
        for (const sel of selectors) {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                if (results.length >= 10) break;
                const style = window.getComputedStyle(el);
                if (style && (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0')) {
                    continue;
                }
                if (el.offsetParent === null && el.tagName !== 'BODY') {
                    continue;
                }
                const raw = el.getAttribute('title') || el.getAttribute('aria-label') || el.innerText || el.textContent || '';
                const txt = cleanText(raw);
                if (txt && txt.length > 2 && !seen.has(txt) && !skippedHeadings.has(txt.toLowerCase())) {
                    seen.add(txt);
                    results.push(txt);
                }
            }
            if (results.length >= 10) break;
        }
    }
    return {
        title: document.title || '',
        url: window.location.href || '',
        items: results.slice(0, 10)
    };
}"""

_GET_BODY_TEXT_SAMPLE_SCRIPT = "() => (document.body ? (document.body.innerText || document.body.textContent || '') : '').slice(0, 300)"

# Frozen set of all permitted JavaScript evaluation scripts
_ALLOWED_EVALUATE_SCRIPTS: set[str] = {
    _MEDIA_PLAY_SCRIPT,
    _MEDIA_PAUSE_SCRIPT,
    _MEDIA_TOGGLE_SCRIPT,
    _MEDIA_MUTE_SCRIPT,
    _MEDIA_UNMUTE_SCRIPT,
    _HAS_VIDEO_SCRIPT,
    _GET_VIDEO_STATE_SCRIPT,
    _GET_SCROLL_POS_SCRIPT,
    _SCROLL_BY_SCRIPT,
    _TAG_RESULTS_SCRIPT,
    _READ_PAGE_SCRIPT,
    _GET_BODY_TEXT_SAMPLE_SCRIPT,
}

_MAX_OPEN_PAGES: int = 5


class PlaywrightBackend(BrowserBackend):
    """Playwright browser automation backend using a persistent browser profile.

    Supports Google Chrome and Chromium, intercepts and aborts private network
    subresources, automatically dismisses dialogs, restricts active tabs to 5,
    runs Chromium with sandbox enabled by default, and verifies outcomes.
    """

    def __init__(
        self,
        profile_dir: Optional[str] = None,
        channel: Optional[str] = None,
        allow_loopback: bool = False,
    ):
        self.profile_dir = profile_dir or getattr(
            settings, "BROWSER_PROFILE_DIR", "data/browser_profile"
        )
        self.channel = (
            channel
            if channel is not None
            else getattr(settings, "BROWSER_CHANNEL", "chrome")
        )
        self.allow_loopback = bool(allow_loopback)

        self._playwright: Any = None
        self._context: Any = None
        self._pages: list[Any] = []
        self._active_page: Any = None
        self.last_failure: Optional[dict[str, Any]] = None
        self.google_blocked_until: float = 0.0
        self.last_route_diagnostic: Optional[str] = None

    def start_google_cooldown(self) -> None:
        mins = getattr(settings, "BROWSER_BLOCK_COOLDOWN_MINUTES", 30)
        self.google_blocked_until = time.time() + (mins * 60)

    def is_google_blocked(self) -> bool:
        return time.time() < self.google_blocked_until

    def _check_verification_page(self) -> None:
        from browser.guards import BrowserRefused, is_blocked_url
        curr_url = self.current_url()
        is_blocked = is_blocked_url(curr_url, allow_loopback=self.allow_loopback)
        if not is_blocked:
            page = self._active_page
            text_sample = ""
            if page is not None and not getattr(page, "is_closed", lambda: False)():
                try:
                    assert _GET_BODY_TEXT_SAMPLE_SCRIPT in _ALLOWED_EVALUATE_SCRIPTS
                    text_sample = str(page.evaluate(_GET_BODY_TEXT_SAMPLE_SCRIPT) or "")[:300]
                except Exception:
                    pass
            if "unusual traffic" in text_sample.lower():
                is_blocked = True

        if is_blocked:
            self.start_google_cooldown()
            try:
                page = self._get_active_page()
                page.go_back(wait_until="domcontentloaded", timeout=3000)
            except Exception:
                pass
            raise BrowserRefused(
                "Google showed a verification page for automated browsing, so I stopped. I will not try to get around it."
            )

    def _map_error(self, exc: Exception) -> Exception:
        """Map Playwright-specific exceptions to friendly ORBIT BrowserErrors."""
        if isinstance(exc, (BrowserRefused, BrowserTimeout, BrowserError)):
            return exc

        msg = str(exc)
        msg_lower = msg.lower()

        if (
            "executable doesn't exist" in msg_lower
            or "could not find browser" in msg_lower
            or "was not found" in msg_lower
            or "browser not installed" in msg_lower
            or ("channel" in msg_lower and "not found" in msg_lower)
        ):
            return BrowserError(
                "Google Chrome was not found; run: playwright install chromium and set BROWSER_CHANNEL= (empty)"
            )

        if (
            "target closed" in msg_lower
            or "target page, context or browser has been closed" in msg_lower
            or "browser has been closed" in msg_lower
            or "page has been closed" in msg_lower
            or "connection closed" in msg_lower
        ):
            return BrowserError(
                "ORBIT's browser was closed and will reopen on the next command"
            )

        if "timeout" in msg_lower or exc.__class__.__name__ == "TimeoutError":
            return BrowserTimeout(f"Browser command timed out: {msg}")

        return BrowserError(f"Browser error: {msg}")

    def _on_new_page(self, page: Any) -> None:
        """Track newly opened pages/popups, dismiss dialogs, and cap open tabs at 5."""
        page.on("dialog", lambda dialog: dialog.dismiss())

        if page not in self._pages:
            self._pages.append(page)
        self._active_page = page

        def _on_close() -> None:
            if page in self._pages:
                self._pages.remove(page)
            if self._active_page == page:
                self._active_page = self._pages[-1] if self._pages else None

        page.on("close", _on_close)

        while len(self._pages) > _MAX_OPEN_PAGES:
            oldest = self._pages.pop(0)
            try:
                oldest.close()
            except Exception:
                pass
            if self._active_page == oldest:
                self._active_page = self._pages[-1] if self._pages else None

    def _get_active_page(self) -> Any:
        """Retrieve current active page or instantiate one if none exist."""
        if self._context is None:
            raise BrowserError("Browser context is not initialized. Call start() first.")

        if self._active_page is not None and not getattr(self._active_page, "is_closed", lambda: False)():
            return self._active_page

        for p in reversed(self._pages):
            if not getattr(p, "is_closed", lambda: False)():
                self._active_page = p
                return p

        try:
            new_p = self._context.new_page()
            self._on_new_page(new_p)
            return new_p
        except Exception as exc:
            raise self._map_error(exc) from exc

    def _route_handler(self, route: Any, request: Any = None) -> None:
        """Network route handler for interception, security filtering, and graceful cancellation."""
        try:
            req_obj = request if request is not None else getattr(route, "request", None)
            url = getattr(req_obj, "url", "") if req_obj is not None else ""
            res_type = getattr(req_obj, "resource_type", "") if req_obj is not None else ""
            is_nav = False
            if hasattr(req_obj, "is_navigation_request"):
                try:
                    is_nav = req_obj.is_navigation_request()
                except Exception:
                    pass
            if not is_nav and res_type in ("document",):
                is_nav = True

            if not getattr(settings, "BROWSER_ALLOW_SIGNIN", False):
                if is_nav and is_signin_url(url, allow_loopback=False):
                    try:
                        route.abort()
                    except BaseException:
                        pass
                    return

            if request_allowed(url, allow_loopback=self.allow_loopback):
                route.continue_()
            else:
                route.abort()
        except (asyncio.CancelledError, Exception, BaseException) as exc:
            self.last_route_diagnostic = (
                f"Route handler intercepted {exc.__class__.__name__}: {str(exc)[:80]}"
            )
            try:
                route.abort()
            except BaseException:
                pass

    def start(self) -> None:
        """Initialize and launch Playwright persistent browser context."""
        profile_path = Path(self.profile_dir).resolve()
        profile_path.mkdir(parents=True, exist_ok=True)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError(
                "Playwright is not installed. Run: pip install -r requirements-browser.txt"
            ) from exc

        launch_sig: Any = None
        sandbox_enabled: bool = bool(getattr(settings, "BROWSER_CHROMIUM_SANDBOX", True))

        try:
            self._playwright = sync_playwright().start()

            channel_val = self.channel if (self.channel and self.channel.strip()) else None

            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_path),
                "channel": channel_val,
                "headless": False,
                "accept_downloads": False,
            }

            try:
                launch_sig = inspect.signature(self._playwright.chromium.launch_persistent_context)
                if "chromium_sandbox" in launch_sig.parameters:
                    launch_kwargs["chromium_sandbox"] = sandbox_enabled
            except Exception:
                pass

            self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)

            self._context.route("**/*", self._route_handler)

            self._context.on("page", self._on_new_page)

            for p in list(self._context.pages):
                self._on_new_page(p)

            if not self._pages:
                new_p = self._context.new_page()
                self._on_new_page(new_p)

        except Exception as exc:
            self.close()
            msg_lower = str(exc).lower()
            if (
                sandbox_enabled
                and launch_sig is not None
                and "chromium_sandbox" in launch_sig.parameters
                and "channel" not in msg_lower
                and "not found" not in msg_lower
                and "executable" not in msg_lower
            ):
                raise BrowserError(
                    f"Chromium failed to start with the sandbox enabled: {exc}. "
                    "You can disable the sandbox by setting BROWSER_CHROMIUM_SANDBOX=False."
                ) from exc
            raise self._map_error(exc) from exc

    def goto(self, url: str) -> None:
        """Navigate the active tab to the specified URL."""
        page = self._get_active_page()
        timeout_ms = int(getattr(settings, "BROWSER_COMMAND_TIMEOUT_S", 20) * 1000)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.bring_to_front()
        except Exception as exc:
            raise self._map_error(exc) from exc
        self._check_verification_page()

    def current_url(self) -> str:
        """Return the current URL of the active browser page."""
        try:
            page = self._get_active_page()
            return str(page.url or "about:blank")
        except Exception as exc:
            raise self._map_error(exc) from exc

    def title(self) -> str:
        """Return the page title of the active browser page."""
        try:
            page = self._get_active_page()
            return str(page.title() or "")
        except Exception as exc:
            raise self._map_error(exc) from exc

    def has_video(self) -> bool:
        """Return True if a video element is present on the page."""
        try:
            page = self._get_active_page()
            assert _HAS_VIDEO_SCRIPT in _ALLOWED_EVALUATE_SCRIPTS
            return bool(page.evaluate(_HAS_VIDEO_SCRIPT))
        except Exception:
            return False

    def click_result(self, n: int, next_video: bool = False, for_shorts: bool = False) -> str:
        """Click the Nth search result or link on the active page (1-indexed)."""
        if not isinstance(n, int) or n < 1 or n > 10:
            raise BrowserRefused(f"Result index must be between 1 and 10, got {n}.")

        page = self._get_active_page()
        timeout_ms = int(getattr(settings, "BROWSER_COMMAND_TIMEOUT_S", 20) * 1000)

        curr_url = self.current_url()
        pt_before = page_type(curr_url, allow_loopback=self.allow_loopback)

        # Polling wait loop up to 4s (every 200 ms) until at least one candidate anchor exists or page stops changing
        # on YouTube also wait for main content container
        start_wait = time.time()
        eval_res: dict[str, Any] = {}
        last_count = -1
        unchanged_cycles = 0

        while (time.time() - start_wait) < 4.0:
            try:
                assert _TAG_RESULTS_SCRIPT in _ALLOWED_EVALUATE_SCRIPTS
                eval_res = page.evaluate(_TAG_RESULTS_SCRIPT, for_shorts) or {}
                count = int(eval_res.get("count", 0))
                if count > 0:
                    break
                if count == last_count:
                    unchanged_cycles += 1
                    if unchanged_cycles >= 3 and (time.time() - start_wait) >= 1.0:
                        break
                else:
                    unchanged_cycles = 0
                    last_count = count
            except Exception:
                pass
            time.sleep(0.2)

        if not eval_res:
            try:
                assert _TAG_RESULTS_SCRIPT in _ALLOWED_EVALUATE_SCRIPTS
                eval_res = page.evaluate(_TAG_RESULTS_SCRIPT, for_shorts) or {}
            except Exception as exc:
                raise self._map_error(exc) from exc

        count = int(eval_res.get("count", 0)) if isinstance(eval_res, dict) else 0
        items = list(eval_res.get("items", [])) if isinstance(eval_res, dict) else []
        diag = eval_res.get("diag", {}) if isinstance(eval_res, dict) else {}
        if diag:
            diag["page_type"] = pt_before

        if count == 0:
            self.last_failure = diag
            exc = BrowserRefused(f"No videos found on this page ({pt_before}). Try scrolling down or searching first.")
            exc.diag = diag
            raise exc

        if n > count or not items:
            raise BrowserRefused(f"I only found {count} results on this page.")

        chosen_item = items[n - 1]
        chosen_title = chosen_item.get("title", f"Result #{n}")
        chosen_href = chosen_item.get("href", "")

        check_url(chosen_href, allow_loopback=self.allow_loopback)

        target_locator = page.locator(f"[data-orbit-result='{n}']").first
        try:
            if hasattr(target_locator, "scroll_into_view_if_needed"):
                target_locator.scroll_into_view_if_needed()
            target_locator.click()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass
        except Exception as exc:
            raise self._map_error(exc) from exc

        url_after = self.current_url()
        self._check_verification_page()
        valid, reason = verify_click_result(curr_url, url_after, allow_loopback=self.allow_loopback)
        if not valid:
            if reason == "signin":
                try:
                    self.history("back")
                except Exception:
                    pass
                raise BrowserRefused(
                    "Google asked to sign in, so I stayed where I was. ORBIT's browser is not signed in."
                )
            raise BrowserRefused("Clicking result did not navigate to expected page.")

        clean_title = " ".join(str(chosen_title).strip().split())
        for ch in ("<", ">", "`"):
            clean_title = clean_title.replace(ch, "")
        clean_title = clean_title[:100].strip()

        return f"Opened result {n}: {clean_title}"

    def key(self, name: str) -> str:
        """Send a keyboard key press on the active page."""
        page = self._get_active_page()
        try:
            page.keyboard.press(name)
            return f"Pressed {name}."
        except Exception as exc:
            raise self._map_error(exc) from exc

    def media(self, action: str) -> str:
        """Execute a media playback action on the first <video> element."""
        act = action.strip().lower()
        if act not in ("play", "pause", "toggle", "mute", "unmute"):
            raise BrowserRefused(
                f"Invalid media action '{action}'. Must be one of: play, pause, toggle, mute, unmute."
            )

        page = self._get_active_page()
        try:
            page.wait_for_selector("video", timeout=5000)
        except Exception as exc:
            raise BrowserError("No video element found on page within 5 seconds.") from exc

        script = _MEDIA_SCRIPTS[act]
        assert script in _ALLOWED_EVALUATE_SCRIPTS
        try:
            page.evaluate(script)
        except Exception as exc:
            raise self._map_error(exc) from exc

        assert _GET_VIDEO_STATE_SCRIPT in _ALLOWED_EVALUATE_SCRIPTS
        try:
            state = page.evaluate(_GET_VIDEO_STATE_SCRIPT) or {}
        except Exception:
            state = {}

        if act == "play" or (act == "toggle" and not state.get("paused")):
            if state.get("paused", True):
                time.sleep(1.5)
                try:
                    state = page.evaluate(_GET_VIDEO_STATE_SCRIPT) or {}
                except Exception:
                    state = {}

                if state.get("paused", True):
                    try:
                        page.keyboard.press("k")
                    except Exception:
                        pass
                    try:
                        state = page.evaluate(_GET_VIDEO_STATE_SCRIPT) or {}
                    except Exception:
                        state = {}

                    if state.get("paused", True):
                        return "The video is open but didn't start; press play."

            if not state.get("paused"):
                return "Playing."
            return "The video is open but didn't start; press play."

        elif act == "pause" or (act == "toggle" and state.get("paused")):
            return "Paused."

        elif act == "mute":
            return "Muted."

        elif act == "unmute":
            return "Unmuted."

        return "Playing."

    def scroll(self, direction: str, screens: float) -> str:
        """Scroll the active viewport by moving mouse to center and wheeling or pressing Home/End."""
        page = self._get_active_page()
        dir_clean = direction.strip().lower()
        if dir_clean not in ("down", "up", "top", "bottom"):
            raise BrowserRefused(
                f"Invalid scroll direction '{direction}'. Must be one of: down, up, top, bottom."
            )

        assert _GET_SCROLL_POS_SCRIPT in _ALLOWED_EVALUATE_SCRIPTS
        try:
            pos_before = page.evaluate(_GET_SCROLL_POS_SCRIPT) or {}
        except Exception:
            pos_before = {"y": 0, "innerHeight": 800, "scrollHeight": 1000}

        y_before = pos_before.get("y", 0)
        inner_h = pos_before.get("innerHeight", 800)
        scroll_h = pos_before.get("scrollHeight", 1000)

        max_y = pos_before.get("maxY", pos_before.get("max_y"))
        if max_y is not None:
            at_bottom = (y_before >= max_y)
        else:
            at_bottom = (y_before + inner_h >= scroll_h - 2)

        if dir_clean in ("down", "bottom") and at_bottom:
            return "Already at the bottom."
        if dir_clean in ("up", "top") and y_before <= 2:
            return "Already at the top."

        try:
            viewport = getattr(page, "viewport_size", None) or {"width": 1280, "height": 800}
            width = viewport.get("width", 1280)
            height = viewport.get("height", 800)
            center_x = width / 2
            center_y = height / 2

            page.mouse.move(center_x, center_y)

            if dir_clean == "top":
                page.keyboard.press("Home")
            elif dir_clean == "bottom":
                page.keyboard.press("End")
            elif dir_clean == "down":
                delta_y = 0.8 * height * float(screens)
                page.mouse.wheel(0, delta_y)
            elif dir_clean == "up":
                delta_y = -0.8 * height * float(screens)
                page.mouse.wheel(0, delta_y)
        except (BrowserRefused, BrowserError):
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

        pos_after = pos_before
        y_after = y_before
        for _ in range(10):
            time.sleep(0.1)
            try:
                pos_after = page.evaluate(_GET_SCROLL_POS_SCRIPT) or {}
                y_after = pos_after.get("y", y_before)
                if y_after != y_before:
                    break
            except Exception:
                pass

        if y_after == y_before:
            if dir_clean == "down":
                fallback_delta = 0.8 * height * float(screens)
            elif dir_clean == "up":
                fallback_delta = -0.8 * height * float(screens)
            elif dir_clean == "bottom":
                fallback_delta = float(scroll_h)
            elif dir_clean == "top":
                fallback_delta = -float(scroll_h)
            else:
                fallback_delta = 0.0

            if fallback_delta != 0.0:
                assert _SCROLL_BY_SCRIPT in _ALLOWED_EVALUATE_SCRIPTS
                try:
                    page.evaluate(_SCROLL_BY_SCRIPT, fallback_delta)
                except Exception:
                    pass

                for _ in range(10):
                    time.sleep(0.1)
                    try:
                        pos_after = page.evaluate(_GET_SCROLL_POS_SCRIPT) or {}
                        y_after = pos_after.get("y", y_before)
                        if y_after != y_before:
                            break
                    except Exception:
                        pass

        if y_after == y_before:
            latest_max_y = pos_after.get("maxY", pos_after.get("max_y"))
            if latest_max_y is not None:
                at_bottom = (y_after >= latest_max_y)
            else:
                latest_inner = pos_after.get("innerHeight", inner_h)
                latest_scroll_h = pos_after.get("scrollHeight", scroll_h)
                at_bottom = (y_after + latest_inner >= latest_scroll_h - 2)

            if dir_clean in ("down", "bottom") and at_bottom:
                return "Already at the bottom."
            if dir_clean in ("up", "top") and y_after <= 2:
                return "Already at the top."
            return "The page didn't scroll."

        diff = int(abs(y_after - y_before))
        if y_after > y_before:
            return f"Scrolled down {diff} px."
        else:
            return f"Scrolled up {diff} px."

    def history(self, action: str) -> str:
        """Navigate page history (back, forward, reload)."""
        page = self._get_active_page()
        timeout_ms = int(getattr(settings, "BROWSER_COMMAND_TIMEOUT_S", 20) * 1000)
        act = action.strip().lower()

        url_before = self.current_url()

        try:
            if act == "back":
                page.go_back(wait_until="domcontentloaded", timeout=timeout_ms)
            elif act == "forward":
                page.go_forward(wait_until="domcontentloaded", timeout=timeout_ms)
            elif act == "reload":
                page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            else:
                raise BrowserRefused(
                    f"Invalid history action '{action}'. Must be one of: back, forward, reload."
                )
        except (BrowserRefused, BrowserError):
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc

        url_after = self.current_url()
        if act == "back" and url_after == url_before:
            return "No earlier page."
        elif act == "forward" and url_after == url_before:
            return "No earlier page."

        self._check_verification_page()
        return page_type(url_after, allow_loopback=self.allow_loopback)

    def read_page(self) -> dict[str, Any]:
        """Collect up to 10 visible result titles or link texts using a fixed script."""
        page = self._get_active_page()
        assert _READ_PAGE_SCRIPT in _ALLOWED_EVALUATE_SCRIPTS

        try:
            raw = page.evaluate(_READ_PAGE_SCRIPT)
            if not isinstance(raw, dict):
                return {
                    "title": self.title(),
                    "url": self.current_url(),
                    "items": [],
                }
            return {
                "title": raw.get("title") or self.title(),
                "url": raw.get("url") or self.current_url(),
                "items": list(raw.get("items") or []),
            }
        except Exception as exc:
            raise self._map_error(exc) from exc

    def close(self) -> None:
        """Close browser context and stop Playwright driver."""
        if self._context is not None:
            try:
                if hasattr(self._context, "unroute_all"):
                    try:
                        self._context.unroute_all(behavior="ignoreErrors")
                    except TypeError:
                        self._context.unroute_all()
            except Exception:
                pass
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self._pages.clear()
        self._active_page = None

    def is_alive(self) -> bool:
        """Return True if browser context is open and responsive."""
        if self._context is None or self._playwright is None:
            return False
        try:
            if hasattr(self._context, "is_closed") and self._context.is_closed():
                return False
            page = self._active_page
            if page is None:
                return False
            if hasattr(page, "is_closed") and page.is_closed():
                return False
            return True
        except Exception:
            return False
