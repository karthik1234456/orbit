"""Tests for browser navigation, web search tools, and URL scheme defenses."""

from typing import Any
from unittest.mock import patch
import pytest

from security.permissions import PermissionLevel
from tools.browser import make_browser_tools as make_classic_browser_tools
from tools.registry import ToolRegistry


class ToolDict(dict):
    """Dictionary of tools with metadata about the implementation under test."""
    impl_type: str = "classic"
    controller: Any = None
    backend: Any = None


@pytest.fixture(params=["classic", "controller"])
def browser_tools(request):
    """Fixture providing browser tools mapped by name, parametrized over classic and controller."""
    impl_type = request.param
    controller = None
    backend = None
    if impl_type == "classic":
        tools = make_classic_browser_tools()
    else:
        from browser.backend import FakeBackend
        from browser.controller import BrowserController
        from tools.browser_tools import make_browser_tools as make_controller_browser_tools
        backend = FakeBackend()
        controller = BrowserController(backend_factory=lambda: backend)
        tools = make_controller_browser_tools(controller)

    d = ToolDict({t.name: t for t in tools})
    d.impl_type = impl_type
    d.controller = controller
    d.backend = backend
    try:
        yield d
    finally:
        if controller is not None:
            controller.shutdown()


def test_tool_metadata_and_permissions(browser_tools):
    """Verify tool permissions: open_website is CONFIRM, search_web is SAFE."""
    assert browser_tools["open_website"].permission == PermissionLevel.CONFIRM
    assert browser_tools["search_web"].permission == PermissionLevel.SAFE
    assert browser_tools["search_site"].permission == PermissionLevel.SAFE


# ---------------------------------------------------------------------------
# open_website tests
# ---------------------------------------------------------------------------


def test_open_website_valid_urls(browser_tools):
    """Verify valid http/https URLs and schemeless domains open with webbrowser.open or controller."""
    with patch("webbrowser.open") as mock_open:
        res = browser_tools["open_website"].func("https://docs.python.org/3/")
        assert "opened website: https://docs.python.org/3/" in res.lower()
        if browser_tools.impl_type == "classic":
            mock_open.assert_called_once_with("https://docs.python.org/3/")
        else:
            mock_open.assert_not_called()
            assert browser_tools.backend.current_url() == "https://docs.python.org/3/"

    with patch("webbrowser.open") as mock_open:
        res = browser_tools["open_website"].func("http://example.com/test")
        assert "opened website: http://example.com/test" in res.lower()
        if browser_tools.impl_type == "classic":
            mock_open.assert_called_once_with("http://example.com/test")
        else:
            mock_open.assert_not_called()
            assert browser_tools.backend.current_url() == "http://example.com/test"

    # Schemeless URL should automatically assume https://
    with patch("webbrowser.open") as mock_open:
        res = browser_tools["open_website"].func("wikipedia.org/wiki/Python")
        assert "opened website: https://wikipedia.org/wiki/python" in res.lower()
        if browser_tools.impl_type == "classic":
            mock_open.assert_called_once_with("https://wikipedia.org/wiki/Python")
        else:
            mock_open.assert_not_called()
            assert browser_tools.backend.current_url() == "https://wikipedia.org/wiki/Python"


@pytest.mark.parametrize(
    "invalid_scheme_url",
    [
        "file:///C:/Windows",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "javascript:void(0)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "ftp://files.example.com",
        "ws://localhost:8080",
    ],
)
def test_open_website_rejects_dangerous_schemes(browser_tools, invalid_scheme_url: str):
    """Verify dangerous schemes (file:, javascript:, data:, ftp:) are rejected."""
    with patch("webbrowser.open") as mock_open:
        res = browser_tools["open_website"].func(invalid_scheme_url)
        assert res.startswith("ERROR:")
        assert "not permitted" in res.lower() or "rejected" in res.lower()
        mock_open.assert_not_called()


@pytest.mark.parametrize(
    "credential_url",
    [
        "https://user:pass@example.com",
        "http://admin:secret@malicious.site/login",
        "https://admin@internal.corp",
        "user:pass@example.com",
    ],
)
def test_open_website_rejects_embedded_credentials(browser_tools, credential_url: str):
    """Verify URLs with user:pass@ or embedded authentication credentials are rejected."""
    with patch("webbrowser.open") as mock_open:
        res = browser_tools["open_website"].func(credential_url)
        assert res.startswith("ERROR:")
        assert "embedded credentials" in res.lower()
        mock_open.assert_not_called()


def test_open_website_rejects_oversized_url(browser_tools):
    """Verify URLs exceeding 2000 characters are rejected."""
    long_url = "https://example.com/" + ("a" * 2005)
    with patch("webbrowser.open") as mock_open:
        res = browser_tools["open_website"].func(long_url)
        assert res.startswith("ERROR:")
        assert "maximum allowed length" in res.lower() or "too long" in res.lower()
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# search_web tests
# ---------------------------------------------------------------------------


def test_search_web_encodes_special_characters_and_spaces(browser_tools):
    """Verify search queries with '&' and spaces are correctly encoded via quote_plus."""
    with patch("webbrowser.open") as mock_open:
        res = browser_tools["search_web"].func("cats & dogs")
        assert "opened search for 'cats & dogs'" in res.lower()

        expected_url = "https://www.google.com/search?q=cats+%26+dogs"
        if browser_tools.impl_type == "classic":
            mock_open.assert_called_once_with(expected_url)
        else:
            mock_open.assert_not_called()
            assert browser_tools.backend.current_url() == expected_url


def test_search_web_complex_symbols_encoded(browser_tools):
    """Verify complex characters, punctuation, and unicode are properly escaped."""
    query = "Python 3.12 + asyncio & typing -> True?"
    with patch("webbrowser.open") as mock_open:
        res = browser_tools["search_web"].func(query)
        assert "opened search" in res.lower()

        if browser_tools.impl_type == "classic":
            mock_open.assert_called_once()
            target_url = mock_open.call_args[0][0]
        else:
            mock_open.assert_not_called()
            target_url = browser_tools.backend.current_url()
        assert target_url.startswith("https://www.google.com/search?q=")
        assert "%26" in target_url  # & encoded
        assert "%2B" in target_url or "+" in target_url  # + / spaces


def test_search_web_rejects_empty_and_oversized_queries(browser_tools):
    """Verify empty queries or queries exceeding 200 characters are rejected."""
    with patch("webbrowser.open") as mock_open:
        res_empty = browser_tools["search_web"].func("   ")
        assert res_empty.startswith("ERROR:")
        assert "cannot be empty" in res_empty.lower()
        mock_open.assert_not_called()

        long_query = "q" * 205
        res_long = browser_tools["search_web"].func(long_query)
        assert res_long.startswith("ERROR:")
        assert "exceeds maximum length" in res_long.lower()
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# search_site tests
# ---------------------------------------------------------------------------


def test_search_site_valid_and_refused(browser_tools):
    """Verify search_site encodes query for allowed site and rejects unknown sites."""
    with patch("webbrowser.open") as mock_open:
        res = browser_tools["search_site"].func("youtube", "lofi beats")
        assert "opened search for 'lofi beats' on youtube in browser." in res.lower()
        expected_url = "https://www.youtube.com/results?search_query=lofi+beats"
        if browser_tools.impl_type == "classic":
            mock_open.assert_called_once_with(expected_url)
        else:
            mock_open.assert_not_called()
            assert browser_tools.backend.current_url() == expected_url

    with patch("webbrowser.open") as mock_open:
        res_unknown = browser_tools["search_site"].func("unknown_site", "test")
        assert res_unknown.startswith("ERROR:")
        assert "not configured for search" in res_unknown
        mock_open.assert_not_called()

    with patch("webbrowser.open") as mock_open:
        res_empty = browser_tools["search_site"].func("youtube", "   ")
        assert res_empty.startswith("ERROR:")
        assert "cannot be empty" in res_empty
        mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# ToolRegistry integration
# ---------------------------------------------------------------------------


def test_browser_tools_registry_integration(browser_tools):
    """Verify browser tools register and execute safely inside ToolRegistry."""
    registry = ToolRegistry()
    for t in browser_tools.values():
        registry.register(t)

    with patch("webbrowser.open") as mock_open:
        res = registry.execute("search_web", {"query": "pytest guide"})
        assert "opened search for 'pytest guide'" in res.lower()
        if browser_tools.impl_type == "classic":
            mock_open.assert_called_once_with("https://www.google.com/search?q=pytest+guide")
        else:
            mock_open.assert_not_called()
            assert browser_tools.backend.current_url() == "https://www.google.com/search?q=pytest+guide"

    with patch("webbrowser.open") as mock_open:
        attack_res = registry.execute("open_website", {"url": "javascript:alert(1)"})
        assert attack_res.startswith("ERROR:")
        mock_open.assert_not_called()


def test_open_website_precheck(browser_tools):
    """Verify open_website precheck returns None for valid URLs, error for invalid ones."""
    tool = browser_tools["open_website"]
    assert tool.precheck is not None

    # Valid URLs return None
    assert tool.precheck({"url": "https://example.com"}) is None
    assert tool.precheck({"url": "http://python.org"}) is None
    assert tool.precheck({"url": "wikipedia.org"}) is None

    # Invalid schemes return error
    err_js = tool.precheck({"url": "javascript:alert(1)"})
    assert err_js is not None and ("not permitted" in err_js or "rejected" in err_js)

    err_file = tool.precheck({"url": "file:///etc/passwd"})
    assert err_file is not None and ("not permitted" in err_file or "rejected" in err_file)

    # Embedded credentials return error
    err_cred = tool.precheck({"url": "https://user:pass@example.com"})
    assert err_cred is not None and "embedded credentials" in err_cred
