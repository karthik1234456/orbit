"""Pytest configuration and global fixtures for ORBIT tests."""

import pytest

from config import settings
import os

# Clean up any oversized environment variables injected by subshells/IDE that exceed Windows 32767 limit
for _k in list(os.environ):
    if len(os.environ[_k]) > 30000:
        del os.environ[_k]

import sys

# Ensure Tcl/Tk libraries are located reliably on Windows
if sys.platform == "win32":
    tcl_path = os.path.join(sys.base_prefix, "tcl", "tcl8.6")
    tk_path = os.path.join(sys.base_prefix, "tcl", "tk8.6")
    if os.path.exists(tcl_path):
        os.environ.setdefault("TCL_LIBRARY", tcl_path)
    if os.path.exists(tk_path):
        os.environ.setdefault("TK_LIBRARY", tk_path)


def make_test_root():
    """Create a withdrawn Tk instance with retry to handle Windows file lock delays.

    Skips the test if Tkinter/Tcl is unavailable (e.g. _tkinter.TclError "Can't find a usable init.tcl").
    Note: The failure was intermittent and environmental.
    """
    import time
    try:
        import tkinter as tk
        for _ in range(5):
            try:
                r = tk.Tk()
                r.withdraw()
                return r
            except Exception:
                time.sleep(0.05)
        r = tk.Tk()
        r.withdraw()
        return r
    except Exception as exc:
        # Note: The failure was intermittent and environmental (e.g. init.tcl missing in subshell).
        pytest.skip(f"Tkinter/Tcl unavailable in environment (intermittent/environmental): {exc}")


def check_tk_available():
    """Verify Tkinter/Tcl is available in the current environment; skip test if unavailable.

    Note: The failure was intermittent and environmental (e.g. TclError "Can't find a usable init.tcl").
    """
    try:
        r = make_test_root()
        r.destroy()
    except Exception as exc:
        pytest.skip(f"Tkinter/Tcl unavailable in environment (intermittent/environmental): {exc}")

# PART A.2: Guard against real browser launches during tests
try:
    import playwright.sync_api

    def _blocked_sync_playwright(*args, **kwargs):
        raise RuntimeError("Real Playwright browser launch is blocked in tests.")

    playwright.sync_api.sync_playwright = _blocked_sync_playwright
except (ImportError, AttributeError):
    pass


@pytest.fixture(autouse=True)
def default_test_settings(monkeypatch):
    """Ensure tests run with clean default settings regardless of local .env."""
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")
    monkeypatch.delenv("ORBIT_OFFLINE", raising=False)
    monkeypatch.setattr(settings, "ORBIT_OFFLINE", False)
    # PART A.1: Default browser mode is OFF for every test
    monkeypatch.setenv("BROWSER_ENABLED", "0")
    monkeypatch.setattr(settings, "PLAN_PREVIEW", "auto")
    monkeypatch.setattr(settings, "CHAINS_ENABLED", True)


@pytest.fixture
def browser_mode(monkeypatch):
    """Explicit fixture enabling browser mode and injecting a FakeBackend factory."""
    monkeypatch.setenv("BROWSER_ENABLED", "1")
    from browser.backend import FakeBackend
    from browser.controller import BrowserController

    factory = lambda: FakeBackend()

    orig_init = BrowserController.__init__

    def _patched_init(self, backend_factory=None, *args, **kwargs):
        if backend_factory is None:
            backend_factory = factory
        return orig_init(self, backend_factory=backend_factory, *args, **kwargs)

    monkeypatch.setattr(BrowserController, "__init__", _patched_init)
    return factory
