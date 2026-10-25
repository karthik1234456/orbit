"""Pytest configuration and global fixtures for ORBIT tests."""

import pytest

from config import settings

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
