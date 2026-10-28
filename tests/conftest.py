"""Pytest configuration and global fixtures for ORBIT tests."""

import pytest

from config import settings
import os

# Clean up any oversized environment variables injected by subshells/IDE that exceed Windows 32767 limit
for _k in list(os.environ):
    if len(os.environ[_k]) > 30000:
        del os.environ[_k]

import sys
from typing import Any

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

# PART A.2: Guard against real browser launches during tests without eager importing
import builtins

_orig_import = builtins.__import__


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "playwright" or name.startswith("playwright."):
        existing = sys.modules.get("playwright")
        if existing is not None and getattr(existing, "__file__", None) is None:
            return _orig_import(name, globals, locals, fromlist, level)
        raise ImportError("Real Playwright browser launch is blocked in tests.")
    return _orig_import(name, globals, locals, fromlist, level)


builtins.__import__ = _guarded_import


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
    monkeypatch.delenv("REMINDERS_ENABLED", raising=False)
    monkeypatch.delenv("COMMAND_SHORTCUTS", raising=False)
    monkeypatch.delenv("TOOL_ROUTING", raising=False)
    monkeypatch.setattr(settings, "REMINDERS_ENABLED", True)
    monkeypatch.setattr(settings, "COMMAND_SHORTCUTS", True)
    monkeypatch.setattr(settings, "TOOL_ROUTING", False)
    monkeypatch.setenv("DOCTOR_ON_START", "0")
    monkeypatch.setattr(settings, "DOCTOR_ON_START", False)


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


# Process protection: tests must never touch real system processes
@pytest.fixture(autouse=True)
def guard_process_iter(monkeypatch):
    """Ensure tests never touch the real process list on the system."""
    import psutil

    def _blocked_process_iter(*args, **kwargs):
        raise AssertionError("test touched real processes; patch core.procs or psutil.process_iter in the test")

    monkeypatch.setattr(psutil, "process_iter", _blocked_process_iter)


# Tk hygiene: track every tkinter.Tk instance and clean up leftovers on main thread
_CREATED_TK_ROOTS: list[Any] = []
_LEFTOVER_TK_TESTS: list[str] = []

try:
    import tkinter as tk

    _orig_tk_init = tk.Tk.__init__

    def _tracking_tk_init(self, *args, **kwargs):
        _orig_tk_init(self, *args, **kwargs)
        _CREATED_TK_ROOTS.append(self)

    tk.Tk.__init__ = _tracking_tk_init

    _orig_var_del = tk.Variable.__del__

    def _safe_var_del(self):
        try:
            _orig_var_del(self)
        except Exception:
            pass

    tk.Variable.__del__ = _safe_var_del
except Exception:
    pass


@pytest.fixture(autouse=True)
def track_tk_roots(request):
    """Track every tkinter.Tk created in a test and destroy leftovers on the main thread."""
    _CREATED_TK_ROOTS.clear()
    yield
    leftovers = []
    for root in list(_CREATED_TK_ROOTS):
        try:
            if root.winfo_exists():
                leftovers.append(root)
        except Exception:
            pass

    if leftovers:
        _LEFTOVER_TK_TESTS.append(request.node.nodeid)
        for root in leftovers:
            try:
                root.destroy()
            except Exception:
                pass
    _CREATED_TK_ROOTS.clear()

