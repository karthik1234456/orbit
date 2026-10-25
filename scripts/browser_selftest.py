"""Automated self-test script for ORBIT browser automation using local FakeTube fixtures.

Serves static mock HTML fixtures on a local loopback server on an ephemeral port
and drives PlaywrightBackend configured with allow_loopback=True through BrowserController.

NOTE: This script is intended for manual execution or dedicated integration checks.
It is NOT executed by pytest.
"""

import http.server
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable

# Ensure project root is in sys.path when executed directly as a script
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from browser.controller import BrowserController
from browser.guards import BrowserRefused, check_url
from browser.playwright_backend import PlaywrightBackend

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "fake_tube"


class FakeTubeHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler serving FakeTube static HTML mock fixtures."""

    def log_message(self, format: str, *args: Any) -> None:
        # Silence HTTP server access logs during selftest
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/results" or path.startswith("/results/"):
            target_file = FIXTURES_DIR / "results.html"
            content_type = "text/html; charset=utf-8"
        elif path == "/watch" or path.startswith("/watch/"):
            target_file = FIXTURES_DIR / "watch.html"
            content_type = "text/html; charset=utf-8"
        elif path == "/signin" or path.startswith("/signin/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            body = (
                b"<!DOCTYPE html><html><head><title>Sign in - FakeTube</title></head>"
                b"<body><h1>Sign in</h1><p>Please enter your credentials.</p></body></html>"
            )
            self.wfile.write(body)
            return
        else:
            target_file = FIXTURES_DIR / "home.html"
            content_type = "text/html; charset=utf-8"

        if not target_file.exists():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Fixture not found")
            return

        with open(target_file, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_stage(name: str, fn: Callable[[], Any]) -> bool:
    """Execute an individual self-test stage and report PASS/FAIL."""
    print(f"[STAGE] {name} ... ", end="", flush=True)
    try:
        output = fn()
        print("PASS")
        if output:
            print(f"        Output: {output}")
        return True
    except Exception as exc:
        print("FAIL")
        print(f"        Error: {type(exc).__name__}: {exc}")
        return False


def run_all_stages() -> bool:
    """Run full FakeTube browser self-test suite and return True if all stages pass."""
    print("=" * 65)
    print("ORBIT Browser Self-Test (FakeTube Loopback Suite)")
    print("=" * 65)

    server = http.server.HTTPServer(("127.0.0.1", 0), FakeTubeHandler)
    host, port = server.server_address
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"Started loopback FakeTube server on http://127.0.0.1:{port}\n")

    # Instantiate controller driving PlaywrightBackend with allow_loopback=True
    controller = BrowserController(
        backend_factory=lambda: PlaywrightBackend(allow_loopback=True)
    )

    passed = 0
    total = 7

    try:
        # Stage 1: Navigate to results page
        def stage_open_results():
            res = controller.navigate(f"http://127.0.0.1:{port}/results")
            assert "/results" in res.get("url", ""), f"Unexpected URL: {res.get('url')}"
            return res.get("title", "")

        if run_stage("1. Navigate to FakeTube results page", stage_open_results):
            passed += 1

        # Stage 2: click_result(2) opens second video and not ad or sign-in link
        def stage_click_second_result():
            res = controller.click_result(2)
            assert "Second Video Result" in res, f"Did not open second video: {res}"
            stat = controller.status()
            url = stat.get("url", "")
            assert "/watch?v=vid2" in url, f"Unexpected target URL: {url}"
            return res

        if run_stage("2. click_result(2) opens second video (ignores ad and sign-in)", stage_click_second_result):
            passed += 1

        # Stage 3: on watch page, click_result(1) opens first recommended video
        def stage_click_recommended():
            res = controller.click_result(1)
            assert "First Recommended Video" in res, f"Did not open recommended video: {res}"
            stat = controller.status()
            url = stat.get("url", "")
            assert "/watch?v=rec1" in url, f"Unexpected target URL: {url}"
            return res

        if run_stage("3. click_result(1) on watch page opens first recommended video", stage_click_recommended):
            passed += 1

        # Stage 4: media play/pause/mute read back real state
        def stage_media_controls():
            play_res = controller.media("play")
            assert play_res == "Playing.", f"Unexpected play state: {play_res}"

            pause_res = controller.media("pause")
            assert pause_res == "Paused.", f"Unexpected pause state: {pause_res}"

            mute_res = controller.media("mute")
            assert mute_res == "Muted.", f"Unexpected mute state: {mute_res}"

            unmute_res = controller.media("unmute")
            assert unmute_res == "Unmuted.", f"Unexpected unmute state: {unmute_res}"

            return f"play={play_res}, pause={pause_res}, mute={mute_res}, unmute={unmute_res}"

        if run_stage("4. media play/pause/mute read back real state", stage_media_controls):
            passed += 1

        # Stage 5: scroll reports a change then 'Already at the bottom.'
        def stage_scroll_verification():
            scrolled_first = False
            res_bottom = ""
            for _ in range(10):
                res = controller.scroll("down", 1.0)
                if "Scrolled down" in res:
                    scrolled_first = True
                elif res == "Already at the bottom.":
                    res_bottom = res
                    break
            assert scrolled_first, "Scroll did not report a change before reaching bottom"
            assert res_bottom == "Already at the bottom.", f"Expected bottom notice: {res_bottom}"

            return f"Scrolled first: {scrolled_first} -> {res_bottom}"

        if run_stage("5. scroll reports a change then 'Already at the bottom.'", stage_scroll_verification):
            passed += 1

        # Stage 6: click on sign-in link ends on original page with sign-in refusal
        def stage_signin_refusal():
            orig_url = controller.status().get("url", "")

            def _click_signin(backend):
                page = backend._get_active_page()
                signin_link = page.locator('a:has-text("Sign in"), #signin-link, a[href*="signin"]').first
                signin_link.click()
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                controller._check_signin_guard(backend)

            try:
                controller._dispatch(_click_signin)
                raise AssertionError("Clicking sign-in link was not refused!")
            except BrowserRefused as exc:
                assert "asked to sign in" in str(exc), f"Unexpected refusal text: {exc}"
                curr_url = controller.status().get("url", "")
                assert "/signin" not in curr_url, f"Browser remained on sign-in URL: {curr_url}"
                return f"Refused ({exc}) and stayed on {curr_url}"

        if run_stage("6. click on sign-in link ends on original page with sign-in refusal", stage_signin_refusal):
            passed += 1

        # Stage 7: navigating to private address from an ordinary run is still refused
        def stage_refuse_private_in_ordinary_run():
            # Private IP must be refused in both ordinary and loopback runs
            try:
                check_url("http://192.168.1.1:8080")
                raise AssertionError("Private IP was not refused in ordinary check_url!")
            except BrowserRefused as exc:
                msg_priv = str(exc)

            # Loopback must be refused in an ordinary run (allow_loopback=False)
            try:
                check_url(f"http://127.0.0.1:{port}/results", allow_loopback=False)
                raise AssertionError("Loopback was not refused in ordinary run!")
            except BrowserRefused as exc:
                msg_loop = str(exc)

            return f"Private refused ({msg_priv}) & Loopback refused in ordinary run ({msg_loop})"

        if run_stage("7. navigating to a private address from an ordinary run is still refused", stage_refuse_private_in_ordinary_run):
            passed += 1

    finally:
        try:
            controller.close()
            controller.shutdown()
        except Exception:
            pass
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass

    print("\n" + "=" * 65)
    print(f"Summary: {passed}/{total} stages passed.")
    print("=" * 65)
    return passed == total


def main() -> int:
    success = run_all_stages()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
