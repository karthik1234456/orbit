"""Pytest configuration and global fixtures for ORBIT tests."""

import pytest

from config import settings


@pytest.fixture(autouse=True)
def default_test_settings(monkeypatch):
    """Ensure tests run with clean default settings regardless of local .env."""
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")
    monkeypatch.delenv("ORBIT_OFFLINE", raising=False)
    monkeypatch.setattr(settings, "ORBIT_OFFLINE", False)
