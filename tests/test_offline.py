"""Tests for core/offline.py network guard and loopback detection."""

import socket
from unittest.mock import MagicMock, patch

import pytest

from core.offline import (
    OfflineViolation,
    disable_network_guard,
    enable_network_guard,
    is_loopback,
    is_network_guard_enabled,
)


@pytest.fixture(autouse=True)
def ensure_network_guard_disabled():
    """Ensure network guard is disabled before and after each test."""
    disable_network_guard()
    yield
    disable_network_guard()


def test_is_loopback_valid_addresses():
    """Verify is_loopback recognizes IPv4, IPv6 loopback and localhost."""
    assert is_loopback("127.0.0.1") is True
    assert is_loopback("127.0.0.2") is True
    assert is_loopback("127.10.20.30") is True
    assert is_loopback("127.255.255.255") is True
    assert is_loopback("::1") is True
    assert is_loopback("localhost") is True
    assert is_loopback("LocalHost") is True
    assert is_loopback("localhost.") is True


def test_is_loopback_rejects_non_loopback_without_dns_resolution():
    """Verify is_loopback returns False for non-loopback IPs and hostnames without DNS lookup."""
    assert is_loopback("example.com") is False
    assert is_loopback("google.com") is False
    assert is_loopback("8.8.8.8") is False
    assert is_loopback("93.184.216.34") is False
    assert is_loopback("192.168.1.1") is False
    assert is_loopback("10.0.0.1") is False
    assert is_loopback("0.0.0.0") is False
    assert is_loopback("::") is False
    assert is_loopback("") is False
    assert is_loopback(None) is False


def test_loopback_addresses_allowed_under_guard():
    """Verify loopback hostnames and IPs are permitted by getaddrinfo, connect, and connect_ex."""
    enable_network_guard()
    assert is_network_guard_enabled() is True

    # getaddrinfo for loopback
    res_ip = socket.getaddrinfo("127.0.0.1", 80)
    assert len(res_ip) > 0

    res_local = socket.getaddrinfo("localhost", 80)
    assert len(res_local) > 0

    # connect_ex to loopback (returns an OS errno e.g. connection refused, does NOT raise OfflineViolation)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        err = sock.connect_ex(("127.0.0.1", 59999))
        assert isinstance(err, int)
    finally:
        sock.close()


def test_public_ip_and_hostname_blocked_without_dns_lookup():
    """Verify public IPs and hostnames are blocked immediately without DNS lookup."""
    enable_network_guard()

    # Track calls to original getaddrinfo to prove no DNS lookup was performed
    with patch("core.offline._orig_getaddrinfo") as mock_orig_getaddrinfo:
        with pytest.raises(OfflineViolation) as exc_info:
            socket.getaddrinfo("example.com", 80)
        assert "ORBIT_OFFLINE is on: blocked connection to example.com" in str(exc_info.value)
        mock_orig_getaddrinfo.assert_not_called()

        with pytest.raises(OfflineViolation) as exc_info:
            socket.getaddrinfo("8.8.8.8", 53)
        assert "ORBIT_OFFLINE is on: blocked connection to 8.8.8.8" in str(exc_info.value)
        mock_orig_getaddrinfo.assert_not_called()

    # Check connect and connect_ex
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OfflineViolation) as exc_info:
            sock.connect(("example.com", 80))
        assert "ORBIT_OFFLINE is on: blocked connection to example.com" in str(exc_info.value)

        with pytest.raises(OfflineViolation) as exc_info:
            sock.connect_ex(("93.184.216.34", 80))
        assert "ORBIT_OFFLINE is on: blocked connection to 93.184.216.34" in str(exc_info.value)
    finally:
        sock.close()


def test_af_unix_allowed(monkeypatch):
    """Verify AF_UNIX domain sockets are permitted when available."""
    # Ensure socket.AF_UNIX is present for test across all platforms
    fake_af_unix = getattr(socket, "AF_UNIX", 9999)
    monkeypatch.setattr(socket, "AF_UNIX", fake_af_unix, raising=False)

    enable_network_guard()

    mock_sock = MagicMock()
    mock_sock.family = fake_af_unix

    with patch("core.offline._orig_connect") as mock_orig_connect:
        mock_orig_connect.return_value = None
        # Connect with path string on unix socket
        socket.socket.connect(mock_sock, "/tmp/orbit.sock")
        mock_orig_connect.assert_called_once_with(mock_sock, "/tmp/orbit.sock")

    with patch("core.offline._orig_connect_ex") as mock_orig_connect_ex:
        mock_orig_connect_ex.return_value = 0
        res = socket.socket.connect_ex(mock_sock, "/tmp/orbit.sock")
        assert res == 0
        mock_orig_connect_ex.assert_called_once_with(mock_sock, "/tmp/orbit.sock")


def test_guard_idempotent():
    """Verify calling enable_network_guard multiple times does not double-wrap."""
    enable_network_guard()
    wrapped_connect = socket.socket.connect
    wrapped_connect_ex = socket.socket.connect_ex
    wrapped_getaddrinfo = socket.getaddrinfo

    enable_network_guard()
    assert socket.socket.connect is wrapped_connect
    assert socket.socket.connect_ex is wrapped_connect_ex
    assert socket.getaddrinfo is wrapped_getaddrinfo


def test_disable_restores_behavior():
    """Verify disable_network_guard restores the original socket methods."""
    from core.offline import _orig_connect, _orig_connect_ex, _orig_getaddrinfo

    enable_network_guard()
    assert is_network_guard_enabled() is True
    assert socket.socket.connect is not _orig_connect

    disable_network_guard()
    assert is_network_guard_enabled() is False
    assert socket.socket.connect is _orig_connect
    assert socket.socket.connect_ex is _orig_connect_ex
    assert socket.getaddrinfo is _orig_getaddrinfo

    # Double disable is safe
    disable_network_guard()
    assert is_network_guard_enabled() is False
