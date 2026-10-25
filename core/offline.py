"""In-process network guard for enforcing ORBIT_OFFLINE restrictions.

NOTE: This is a best-effort in-process guard, not an operating system firewall.
It intercepts socket connections and address resolution in the Python runtime
to prevent outbound network connections while in offline mode.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Optional


class OfflineViolation(Exception):
    """Raised when an outbound connection to a non-loopback host is attempted in offline mode."""
    pass


def is_loopback(host: Any) -> bool:
    """Return True if host is a loopback address, False otherwise.

    True for 127.0.0.0/8, ::1, and "localhost".
    A hostname other than localhost is NOT loopback and must not be resolved.
    """
    if host is None:
        return False
    host_str = str(host).strip().rstrip(".").lower()
    if host_str == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host_str)
        return ip.is_loopback
    except ValueError:
        return False


_guard_enabled: bool = False
_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_getaddrinfo = socket.getaddrinfo


def _is_af_unix_socket(sock: socket.socket, address: Any) -> bool:
    """Check if the socket or address corresponds to an AF_UNIX domain socket."""
    af_unix = getattr(socket, "AF_UNIX", None)
    if af_unix is not None:
        if getattr(sock, "family", None) == af_unix:
            return True
        if isinstance(address, (str, bytes)):
            return True
    return False


def _extract_host(address: Any) -> Optional[str]:
    """Extract the hostname or IP string from a socket address tuple."""
    if isinstance(address, (tuple, list)) and len(address) > 0:
        return str(address[0])
    return None


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    """Wrapper for socket.socket.connect enforcing offline loopback restrictions."""
    if _is_af_unix_socket(self, address):
        return _orig_connect(self, address)

    host = _extract_host(address)
    if host is not None and is_loopback(host):
        return _orig_connect(self, address)

    blocked_target = host if host is not None else address
    raise OfflineViolation(f"ORBIT_OFFLINE is on: blocked connection to {blocked_target}")


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    """Wrapper for socket.socket.connect_ex enforcing offline loopback restrictions."""
    if _is_af_unix_socket(self, address):
        return _orig_connect_ex(self, address)

    host = _extract_host(address)
    if host is not None and is_loopback(host):
        return _orig_connect_ex(self, address)

    blocked_target = host if host is not None else address
    raise OfflineViolation(f"ORBIT_OFFLINE is on: blocked connection to {blocked_target}")


def _guarded_getaddrinfo(
    host: Optional[str],
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[Any]:
    """Wrapper for socket.getaddrinfo enforcing offline loopback restrictions."""
    af_unix = getattr(socket, "AF_UNIX", None)
    if af_unix is not None and family == af_unix:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    if host is None or host == "":
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    if is_loopback(host):
        return _orig_getaddrinfo(host, port, family, type, proto, flags)

    raise OfflineViolation(f"ORBIT_OFFLINE is on: blocked connection to {host}")


def enable_network_guard() -> None:
    """Enable in-process network guard.

    Replaces socket.socket.connect, socket.socket.connect_ex, and socket.getaddrinfo
    with wrappers permitting AF_UNIX and loopback hosts only.
    Idempotent: calling multiple times will not double-wrap.
    """
    global _guard_enabled
    if _guard_enabled:
        return

    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.getaddrinfo = _guarded_getaddrinfo
    _guard_enabled = True


def disable_network_guard() -> None:
    """Disable in-process network guard and restore original socket functions."""
    global _guard_enabled
    if not _guard_enabled:
        return

    socket.socket.connect = _orig_connect
    socket.socket.connect_ex = _orig_connect_ex
    socket.getaddrinfo = _orig_getaddrinfo
    _guard_enabled = False


def is_network_guard_enabled() -> bool:
    """Return True if the network guard is currently active."""
    return _guard_enabled
