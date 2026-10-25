"""Manual diagnostic verification script for ORBIT browser automation.

NOTE: This script is intended for interactive manual testing by the developer
and is NEVER executed by pytest or automated CI suites. It requires a live
display and an installed browser (Google Chrome or Playwright Chromium).

Stages executed:
1. Open youtube.com
2. Search "lofi music"
3. Click result 1
4. Media toggle
5. Media mute
6. Scroll down
7. Read page (print titles)
8. History back
9. Refuse localhost (check_url)
10. Close browser
"""

import sys
import time
from typing import Any, Callable

from browser.controller import BrowserController
from browser.guards import BrowserRefused, check_url
from browser.playwright_backend import PlaywrightBackend


def run_stage(name: str, fn: Callable[[], Any]) -> bool:
    """Execute a single verification stage, print PASS/FAIL, and continue."""
    print(f"\n[STAGE] {name} ... ", end="", flush=True)
    try:
        res = fn()
        print("PASS")
        if res is not None:
            print(f"        Output: {res}")
        return True
    except Exception as exc:
        print("FAIL")
        print(f"        Error: {type(exc).__name__}: {exc}")
        return False


def run_all_stages() -> None:
    print("=" * 60)
    print("ORBIT Browser Diagnostic Check (Manual Verification)")
    print("=" * 60)

    controller = BrowserController(backend_factory=lambda: PlaywrightBackend())
    passed = 0
    total = 10

    # 1. Open youtube.com
    def stage_open_youtube():
        res = controller.navigate("https://www.youtube.com")
        assert "youtube.com" in res.get("url", ""), f"Expected URL containing youtube.com, got {res.get('url')}"
        return res.get("title", "")

    if run_stage("1. Open youtube.com", stage_open_youtube):
        passed += 1
    time.sleep(1.0)

    # 2. Search "lofi music"
    def stage_search_lofi():
        res = controller.search("youtube", "lofi music")
        assert "/results" in res.get("url", ""), f"Expected URL containing /results, got {res.get('url')}"
        return res.get("url", "")

    if run_stage("2. Search 'lofi music'", stage_search_lofi):
        passed += 1
    time.sleep(2.0)

    # 3. Click result 1
    def stage_click_result():
        res = controller.click_result(1)
        stat = controller.status()
        url = stat.get("url", "")
        title = stat.get("title", "")
        assert "/watch" in url, f"Expected URL containing /watch, got {url}"
        assert bool(title.strip()), f"Expected non-empty page title, got {title!r}"
        return f"{res} -> {url} ({title})"

    if run_stage("3. Click result 1", stage_click_result):
        passed += 1
    time.sleep(2.0)

    # 4. Media toggle
    def stage_media_toggle():
        res1 = controller.media("toggle")
        assert res1 in ("Playing.", "Paused."), f"Unexpected state from toggle 1: {res1}"
        time.sleep(1.0)
        res2 = controller.media("toggle")
        assert res2 in ("Playing.", "Paused."), f"Unexpected state from toggle 2: {res2}"
        assert res1 != res2, f"Expected returned state to change, got {res1} and {res2}"
        return f"{res1} -> {res2}"

    if run_stage("4. Media toggle", stage_media_toggle):
        passed += 1
    time.sleep(1.0)

    # 5. Media mute
    def stage_media_mute():
        res = controller.media("mute")
        assert res == "Muted.", f"Expected 'Muted.', got {res}"
        return res

    if run_stage("5. Media mute", stage_media_mute):
        passed += 1
    time.sleep(0.5)

    # 6. Scroll down
    def stage_scroll_down():
        res = controller.scroll("down", 1.0)
        assert "Scrolled down" in res, f"Expected scroll position change, got {res}"
        return res

    if run_stage("6. Scroll down", stage_scroll_down):
        passed += 1
    time.sleep(0.5)

    # 7. Read page (print titles)
    def stage_read_page():
        info = controller.read_page()
        print(f"\n        Page Title: {info.get('title')}")
        print(f"        Page URL: {info.get('url')}")
        items = info.get("items", [])
        print(f"        Visible Items ({len(items)}):")
        for idx, item in enumerate(items, start=1):
            print(f"          {idx}. {item}")
        assert len(items) > 0, "Expected at least one item read from page"
        return f"{len(items)} items read"

    if run_stage("7. Read page (print titles)", stage_read_page):
        passed += 1

    # 8. History back
    def stage_history_back():
        res = controller.history("back")
        assert res in ("youtube_results", "youtube_home", "youtube_watch", "youtube_other"), f"Unexpected history outcome: {res}"
        return res

    if run_stage("8. History back", stage_history_back):
        passed += 1
    time.sleep(1.0)

    # 9. Refuse localhost (check_url)
    def stage_refuse_localhost():
        try:
            check_url("http://localhost:8080")
            raise AssertionError("check_url failed to refuse localhost URL!")
        except BrowserRefused as exc:
            return f"Correctly refused: {exc}"

    if run_stage("9. Refuse localhost (check_url)", stage_refuse_localhost):
        passed += 1

    # 10. Close
    def stage_close():
        controller.close()
        controller.shutdown()
        return "Closed"

    if run_stage("10. Close browser", stage_close):
        passed += 1

    print("\n" + "=" * 60)
    print(f"Summary: {passed}/{total} stages passed.")
    print("=" * 60)


if __name__ == "__main__":
    run_all_stages()
