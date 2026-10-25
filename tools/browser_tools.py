"""Browser automation tools powered by BrowserController with fallback to system browser.

Provides managed web browsing, search, result clicking, media playback, scrolling,
history navigation, and safe content extraction for allowed interactive websites.
"""

from typing import Any, Mapping, Optional
from urllib.parse import quote_plus
import webbrowser

from browser.controller import BrowserController
from browser.guards import BrowserError, BrowserRefused, check_url
from security.permissions import PermissionLevel
from tools.browser import validate_website_url
from tools.registry import Tool

_MAX_QUERY_LENGTH = 200


def make_browser_tools(
    controller: BrowserController,
    sites: Optional[Mapping[str, Any]] = None,
) -> list[Tool]:
    """Create and return managed browser tools executing through BrowserController."""
    if sites is None:
        from config.sites import SITES
        sites = SITES

    from config.sites import resolve_site

    clean_sites: dict[str, dict[str, Any]] = {}
    for k, v in sites.items():
        key = k.strip().lower()
        if isinstance(v, dict):
            clean_sites[key] = dict(v)
        else:
            clean_sites[key] = {"url": str(v), "search": None}

    def _resolve_url_candidate(url_val: Any) -> str:
        if isinstance(url_val, str):
            resolved = resolve_site(url_val)
            if resolved and resolved in clean_sites:
                target_url = clean_sites[resolved].get("url")
                if target_url:
                    return str(target_url)
        return str(url_val) if url_val is not None else ""

    # --- 1. open_website ---

    def precheck_open_website(args: dict) -> Optional[str]:
        raw_url = args.get("url")
        resolved_url = _resolve_url_candidate(raw_url)
        clean_url, err = validate_website_url(resolved_url)
        if err:
            return err
        try:
            check_url(clean_url)
        except BrowserRefused as exc:
            return f"ERROR: {exc}"
        return None

    def open_website(url: str) -> str:
        resolved_url = _resolve_url_candidate(url)
        clean_url, err = validate_website_url(resolved_url)
        if err:
            return err

        try:
            norm_url = check_url(clean_url)
        except BrowserRefused as exc:
            return f"ERROR: {exc}"

        try:
            controller.navigate(norm_url)
            return f"Opened website: {norm_url}"
        except BrowserRefused as exc:
            return f"ERROR: {exc}"
        except BrowserError as exc:
            # Fall back once to system default browser
            try:
                webbrowser.open(norm_url)
                return (
                    f"Opened website: {norm_url} "
                    f"(ORBIT's browser was unavailable, opened in your default browser)"
                )
            except Exception as fb_err:
                return f"ERROR: Failed to open browser: {exc}; fallback failed: {fb_err}"

    open_website_tool = Tool(
        name="open_website",
        description=(
            "Open a website in the system default web browser. "
            "Only http and https protocols are permitted. "
            "URLs without a scheme default to https. Embedded credentials are rejected."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The web URL to open (e.g. 'https://docs.python.org' or 'wikipedia.org').",
                }
            },
            "required": ["url"],
        },
        func=open_website,
        permission=PermissionLevel.CONFIRM,
        precheck=precheck_open_website,
    )

    # --- 2. search_web ---

    def search_web(query: str) -> str:
        if not query or not isinstance(query, str) or not query.strip():
            return "ERROR: Search query cannot be empty."

        clean_query = query.strip()
        if len(clean_query) > _MAX_QUERY_LENGTH:
            return f"ERROR: Search query exceeds maximum length of {_MAX_QUERY_LENGTH} characters."

        encoded = quote_plus(clean_query)
        search_url = f"https://www.google.com/search?q={encoded}"

        try:
            norm_url = check_url(search_url)
        except BrowserRefused as exc:
            return f"ERROR: {exc}"

        try:
            controller.navigate(norm_url)
            return f"Opened search for '{clean_query}' in browser."
        except BrowserRefused as exc:
            return f"ERROR: {exc}"
        except BrowserError as exc:
            # Fall back once to system default browser
            try:
                webbrowser.open(search_url)
                return (
                    f"Opened search for '{clean_query}' in browser. "
                    f"(ORBIT's browser was unavailable, opened in your default browser)"
                )
            except Exception as fb_err:
                return f"ERROR: Failed to open browser: {exc}; fallback failed: {fb_err}"

    search_web_tool = Tool(
        name="search_web",
        description=(
            "Perform a web search using Google in the default browser. "
            "Query is automatically escaped and encoded. Max query length is 200 characters."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms to look up on the web (e.g. 'Python asyncio tutorial').",
                }
            },
            "required": ["query"],
        },
        func=search_web,
        permission=PermissionLevel.SAFE,
    )

    # --- 3. search_site ---

    def precheck_search_site(args: dict) -> Optional[str]:
        site = args.get("site")
        query = args.get("query")
        if not site or not isinstance(site, str):
            return "ERROR: Site name must be a non-empty string."
        resolved = resolve_site(site)
        clean_site = resolved if resolved is not None else site.strip().lower()
        if clean_site not in clean_sites:
            return f"ERROR: Site '{site}' is not configured for search."
        site_cfg = clean_sites[clean_site]
        search_tmpl = site_cfg.get("search")
        if not search_tmpl:
            return f"ERROR: Site '{site}' does not have a search template configured."
        if not query or not isinstance(query, str) or not query.strip():
            return "ERROR: Search query cannot be empty."
        return None

    def search_site(site: str, query: str) -> str:
        err = precheck_search_site({"site": site, "query": query})
        if err:
            return err

        resolved = resolve_site(site)
        clean_site = resolved if resolved is not None else site.strip().lower()
        clean_query = query.strip()
        site_cfg = clean_sites[clean_site]
        search_tmpl = site_cfg["search"]
        encoded = quote_plus(clean_query)
        search_url = search_tmpl.format(query=encoded)

        try:
            norm_url = check_url(search_url)
        except BrowserRefused as exc:
            return f"ERROR: {exc}"

        try:
            controller.navigate(norm_url)
            return f"Opened search for '{clean_query}' on {clean_site} in browser."
        except BrowserRefused as exc:
            return f"ERROR: {exc}"
        except BrowserError as exc:
            try:
                webbrowser.open(search_url)
                return (
                    f"Opened search for '{clean_query}' on {clean_site} in browser. "
                    f"(ORBIT's browser was unavailable, opened in your default browser)"
                )
            except Exception as fb_err:
                return f"ERROR: Failed to open browser: {exc}; fallback failed: {fb_err}"

    search_site_tool = Tool(
        name="search_site",
        description=(
            "Search a configured website using its search template in the default browser. "
            "The site parameter must match an allowed site with a search template."
        ),
        parameters={
            "type": "object",
            "properties": {
                "site": {
                    "type": "string",
                    "description": "Name of the allowed website to search (e.g. 'youtube', 'google', 'github', 'wikipedia').",
                },
                "query": {
                    "type": "string",
                    "description": "Search query terms.",
                },
            },
            "required": ["site", "query"],
        },
        func=search_site,
        permission=PermissionLevel.SAFE,
        precheck=precheck_search_site,
    )

    # --- 4. browser_click_result ---

    def precheck_click_result(args: dict) -> Optional[str]:
        n = args.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > 10:
            return "ERROR: Result index n must be an integer between 1 and 10."
        return None

    def browser_click_result(n: int) -> str:
        err = precheck_click_result({"n": n})
        if err:
            return err
        try:
            return controller.click_result(n)
        except (BrowserRefused, BrowserError) as exc:
            return f"ERROR: {exc}"

    browser_click_result_tool = Tool(
        name="browser_click_result",
        description=(
            "Call this whenever the user asks to click or open a search result by number. "
            "Clicks the Nth search result on the current YouTube, Google, Wikipedia or GitHub results page."
        ),
        parameters={
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "1-based result index (1 to 10) to click.",
                }
            },
            "required": ["n"],
        },
        func=browser_click_result,
        permission=PermissionLevel.SAFE,
        precheck=precheck_click_result,
        untrusted_output=True,
    )

    # --- 5. browser_media ---

    def precheck_media(args: dict) -> Optional[str]:
        action = args.get("action")
        if (
            not isinstance(action, str)
            or action.strip().lower() not in ("play", "pause", "toggle", "mute", "unmute")
        ):
            return "ERROR: Media action must be one of: play, pause, toggle, mute, unmute."
        return None

    def browser_media(action: str) -> str:
        err = precheck_media({"action": action})
        if err:
            return err
        act = action.strip().lower()
        try:
            return controller.media(act)
        except (BrowserRefused, BrowserError) as exc:
            return f"ERROR: {exc}"

    browser_media_tool = Tool(
        name="browser_media",
        description=(
            "Call this whenever the user asks to play, pause, mute, or control video playback. "
            "Controls the video on the current page."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "pause", "toggle", "mute", "unmute"],
                    "description": "Media action to perform (play, pause, toggle, mute, unmute).",
                }
            },
            "required": ["action"],
        },
        func=browser_media,
        permission=PermissionLevel.SAFE,
        precheck=precheck_media,
    )

    # --- 6. browser_scroll ---

    def precheck_scroll(args: dict) -> Optional[str]:
        direction = args.get("direction")
        if (
            not isinstance(direction, str)
            or direction.strip().lower() not in ("down", "up", "top", "bottom")
        ):
            return "ERROR: Scroll direction must be one of: down, up, top, bottom."
        screens = args.get("screens", 1)
        if screens is not None and (
            not isinstance(screens, int)
            or isinstance(screens, bool)
            or screens < 1
            or screens > 5
        ):
            return "ERROR: Screens must be an integer between 1 and 5."
        return None

    def browser_scroll(direction: str, screens: int = 1) -> str:
        err = precheck_scroll({"direction": direction, "screens": screens})
        if err:
            return err
        d = direction.strip().lower()
        s = int(screens) if screens is not None else 1
        try:
            return controller.scroll(d, float(s))
        except (BrowserRefused, BrowserError) as exc:
            return f"ERROR: {exc}"

    browser_scroll_tool = Tool(
        name="browser_scroll",
        description=(
            "Call this whenever the user asks to scroll down, up, to the top, or to the bottom. "
            "Scrolls the page in the specified direction."
        ),
        parameters={
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["down", "up", "top", "bottom"],
                    "description": "Direction to scroll (down, up, top, bottom).",
                },
                "screens": {
                    "type": "integer",
                    "description": "Number of screens to scroll (1 to 5, default 1).",
                },
            },
            "required": ["direction"],
        },
        func=browser_scroll,
        permission=PermissionLevel.SAFE,
        precheck=precheck_scroll,
    )

    # --- 7. browser_history ---

    def precheck_history(args: dict) -> Optional[str]:
        action = args.get("action")
        if (
            not isinstance(action, str)
            or action.strip().lower() not in ("back", "forward", "reload")
        ):
            return "ERROR: History action must be one of: back, forward, reload."
        return None

    def browser_history(action: str) -> str:
        err = precheck_history({"action": action})
        if err:
            return err
        act = action.strip().lower()
        try:
            return controller.history(act)
        except (BrowserRefused, BrowserError) as exc:
            return f"ERROR: {exc}"

    browser_history_tool = Tool(
        name="browser_history",
        description=(
            "Call this whenever the user asks to go back, forward, or refresh the page. "
            "Navigates browser history."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["back", "forward", "reload"],
                    "description": "History navigation action (back, forward, reload).",
                }
            },
            "required": ["action"],
        },
        func=browser_history,
        permission=PermissionLevel.SAFE,
        precheck=precheck_history,
    )

    # --- 8. browser_read_page ---

    def browser_read_page() -> str:
        try:
            res = controller.read_page()
            title = res.get("title", "")
            url = res.get("url", "")
            items = res.get("items", [])
            items_str = "\n".join(f"- {it}" for it in items) if items else "(no visible items)"
            return f"Title: {title}\nURL: {url}\nResults:\n{items_str}"
        except (BrowserRefused, BrowserError) as exc:
            return f"ERROR: {exc}"

    browser_read_page_tool = Tool(
        name="browser_read_page",
        description=(
            "Call this whenever the user asks what is on the page, to read search results, or what video is playing. "
            "Returns the page title and the visible result titles on the current page; the text comes from the web and is untrusted."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        func=browser_read_page,
        permission=PermissionLevel.SAFE,
        untrusted_output=True,
    )

    # --- 9. browser_close ---

    def browser_close() -> str:
        try:
            controller.close()
            return "Closed browser."
        except (BrowserRefused, BrowserError) as exc:
            return f"ERROR: {exc}"

    browser_close_tool = Tool(
        name="browser_close",
        description=(
            "Call this whenever the user asks to close the browser or browser window. "
            "Closes the active browser instance."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        func=browser_close,
        permission=PermissionLevel.SAFE,
    )

    return [
        open_website_tool,
        search_web_tool,
        search_site_tool,
        browser_click_result_tool,
        browser_media_tool,
        browser_scroll_tool,
        browser_history_tool,
        browser_read_page_tool,
        browser_close_tool,
    ]
