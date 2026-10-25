"""Security guards, URL validation, and network policy for ORBIT browser automation.

Enforces strict URL format checks, SSRF prevention, private host filtering,
and interactive site domain whitelisting.
"""

import ipaddress
import re
from typing import Any, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from config.browser_sites import NON_INTERACTIVE_HOSTS, interactive_hosts


_GOOGLE_HOST_RE = re.compile(
    r"^(www\.)?google\.(com|co\.[a-z]{2}|com\.[a-z]{2}|[a-z]{2})$",
    re.IGNORECASE,
)


def is_google_host(hostname: str) -> bool:
    """Return True if hostname matches google.com or Google country domains."""
    if not hostname:
        return False
    return bool(_GOOGLE_HOST_RE.match(hostname.strip().lower()))


class BrowserRefused(Exception):
    """Raised when a browser action, URL, or interaction is refused for security or policy reasons."""
    pass


class BrowserError(Exception):
    """Raised when a browser backend encounters an operational error or crash."""
    pass


class BrowserTimeout(BrowserError):
    """Raised when a browser command exceeds the allowed timeout limit."""
    pass


def _is_prohibited_host(hostname: str, allow_loopback: bool = False) -> Tuple[bool, str]:
    """Check if a hostname is private, loopback, link-local, reserved, or obfuscated numeric.

    Returns (is_prohibited, reason).
    """
    if not hostname:
        return True, "Missing host or domain name."

    clean_host = hostname.strip().rstrip(".").lower()

    # Block localhost and local/internal domain suffixes
    if clean_host in ("localhost", "local", "internal"):
        if allow_loopback and clean_host == "localhost":
            return False, ""
        return True, f"Access to '{clean_host}' is blocked."
    if clean_host.endswith(".localhost") or clean_host.endswith(".local") or clean_host.endswith(".internal"):
        if allow_loopback and clean_host.endswith(".localhost"):
            return False, ""
        return True, f"Access to internal domain '{clean_host}' is blocked."

    # Block all-digit numeric hosts (e.g. http://2130706433/)
    if clean_host.isdigit():
        return True, f"All-digit numeric host '{clean_host}' is blocked."

    # Block hex numeric hosts (e.g. http://0x7f000001/)
    if clean_host.startswith("0x") or clean_host.startswith("0X"):
        return True, f"Hex numeric host '{clean_host}' is blocked."

    # Check for hex or octal labels in dot-separated host (e.g. 0177.0.0.1 or 0x7f.0.0.1)
    labels = clean_host.split(".")
    for label in labels:
        if label.startswith("0x") or label.startswith("0X"):
            return True, f"Hex-encoded host label '{label}' is blocked."
        if len(label) > 1 and label.startswith("0") and label.isdigit():
            return True, f"Octal-encoded host label '{label}' is blocked."

    # Check if the entire host evaluates as a numeric literal with base 0
    try:
        int(clean_host, 0)
        return True, f"Numeric host literal '{clean_host}' is blocked."
    except (ValueError, TypeError):
        pass

    # Check IP address ranges (IPv4, IPv6, and IPv4-mapped IPv6)
    ip_str = clean_host.strip("[]")
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        ip = None

    if ip is not None:
        if ip.is_loopback:
            if allow_loopback:
                return False, ""
            return True, f"Loopback IP address '{clean_host}' is blocked."
        if ip.is_private:
            return True, f"Private IP address '{clean_host}' is blocked."
        if ip.is_link_local:
            return True, f"Link-local IP address '{clean_host}' is blocked."
        if ip.is_reserved:
            return True, f"Reserved IP address '{clean_host}' is blocked."
        if ip.is_multicast:
            return True, f"Multicast IP address '{clean_host}' is blocked."
        if ip.is_unspecified:
            return True, f"Unspecified IP address '{clean_host}' is blocked."

        # IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            if mapped.is_loopback and allow_loopback:
                pass
            elif (
                mapped.is_loopback
                or mapped.is_private
                or mapped.is_link_local
                or mapped.is_reserved
                or mapped.is_multicast
                or mapped.is_unspecified
            ):
                return True, f"Prohibited IPv4-mapped address '{clean_host}' is blocked."

    return False, ""


def check_url(url: Any, allow_loopback: bool = False) -> str:
    """Validate and normalize a URL for browser navigation.

    Returns the normalized URL string.
    Raises BrowserRefused if the URL is invalid, non-http(s), contains credentials,
    exceeds 2000 characters, or targets prohibited/internal/loopback/private hosts.
    """
    if not url or not isinstance(url, str):
        raise BrowserRefused("URL must be a non-empty string.")

    clean_url = url.strip()
    if len(clean_url) > 2000:
        raise BrowserRefused("URL exceeds maximum length of 2000 characters.")

    if "@" in clean_url:
        raise BrowserRefused("URLs with embedded credentials are not permitted.")

    parsed = urlparse(clean_url)
    if not parsed.scheme:
        clean_url = f"https://{clean_url}"
        parsed = urlparse(clean_url)

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise BrowserRefused(
            f"URL scheme '{scheme}:' is not permitted. Only http and https protocols are allowed."
        )

    hostname = parsed.hostname
    if not hostname:
        raise BrowserRefused("Invalid URL: missing host or domain name.")

    prohibited, reason = _is_prohibited_host(hostname, allow_loopback=allow_loopback)
    if prohibited:
        raise BrowserRefused(f"Prohibited host in URL: {reason}")

    # Normalize scheme and host
    norm_host = hostname.lower()
    if parsed.port:
        norm_netloc = f"{norm_host}:{parsed.port}"
    else:
        norm_netloc = norm_host

    normalized = urlunparse((
        scheme,
        norm_netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))
    return normalized


def request_allowed(url: Any, allow_loopback: bool = False) -> bool:
    """Determine whether an intercepted subresource or network request is allowed.

    Permits http, https, data, blob, and about schemes. Blocks private and loopback hosts.
    """
    if not url or not isinstance(url, str):
        return False

    clean_url = url.strip()
    try:
        parsed = urlparse(clean_url)
    except Exception:
        return False

    scheme = (parsed.scheme or "").lower()
    if scheme in ("data", "blob", "about"):
        if scheme == "blob" and "://" in parsed.path:
            try:
                inner = urlparse(parsed.path)
                if inner.hostname:
                    prohibited, _ = _is_prohibited_host(inner.hostname, allow_loopback=allow_loopback)
                    if prohibited:
                        return False
            except Exception:
                return False
        return True

    if scheme in ("http", "https"):
        hostname = parsed.hostname
        if not hostname:
            return False
        prohibited, _ = _is_prohibited_host(hostname, allow_loopback=allow_loopback)
        return not prohibited

    return False


def is_interactive(url: Any, allow_loopback: bool = False) -> bool:
    """Return True if the URL belongs to an allowed interactive site."""
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
        hostname = (parsed.hostname or "").strip().lower()
    except Exception:
        return False

    if not hostname:
        return False

    if allow_loopback and hostname in ("127.0.0.1", "localhost"):
        return True

    # Block explicitly non-interactive hosts (e.g. mail.google.com, web.whatsapp.com)
    for nih in NON_INTERACTIVE_HOSTS:
        if hostname == nih or hostname.endswith("." + nih):
            return False

    if is_google_host(hostname):
        return True

    hosts = interactive_hosts()
    if hostname in hosts:
        return True

    for h in hosts:
        if hostname == h or hostname.endswith("." + h):
            return True

    return False


def page_type(url: Any, allow_loopback: bool = False) -> str:
    """Classify a URL into one of the known interactive site page types or 'unknown'.

    Returns one of:
    - youtube_home
    - youtube_results
    - youtube_watch
    - youtube_other
    - google_results
    - google_other
    - wikipedia_results
    - wikipedia_article
    - github_results
    - github_other
    - unknown
    """
    if not url or not isinstance(url, str):
        return "unknown"

    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
    except Exception:
        return "unknown"

    if not host:
        return "unknown"

    # FakeTube on loopback
    if allow_loopback and host in ("127.0.0.1", "localhost"):
        norm_path = path.rstrip("/") if path != "/" else path
        if norm_path in ("", "/", "/home"):
            return "youtube_home"
        if norm_path == "/results" or norm_path.startswith("/results/"):
            return "youtube_results"
        if norm_path == "/watch" or norm_path.startswith("/watch/"):
            return "youtube_watch"
        if norm_path == "/shorts" or norm_path.startswith("/shorts/"):
            return "youtube_shorts"
        return "youtube_other"

    # YouTube
    if host == "youtube.com" or host.endswith(".youtube.com") or host in ("youtu.be", "www.youtu.be"):
        norm_path = path.rstrip("/") if path != "/" else path
        if norm_path in ("", "/"):
            return "youtube_home"
        if norm_path == "/results" or norm_path.startswith("/results/"):
            return "youtube_results"
        if norm_path == "/watch" or norm_path.startswith("/watch/"):
            return "youtube_watch"
        if norm_path == "/shorts" or norm_path.startswith("/shorts/"):
            return "youtube_shorts"
        return "youtube_other"

    # Google
    if is_google_host(host) or host == "google.com" or host.endswith(".google.com"):
        norm_path = path.rstrip("/") if path != "/" else path
        if norm_path == "/search" or norm_path.startswith("/search/"):
            return "google_results"
        return "google_other"

    # Wikipedia
    if host == "wikipedia.org" or host.endswith(".wikipedia.org"):
        norm_path = path.rstrip("/") if path != "/" else path
        query = parsed.query or ""
        if (
            (norm_path == "/w/index.php" or norm_path.startswith("/w/index.php"))
            and ("search=" in query or "search" in query)
        ) or (
            norm_path in ("/search", "/wiki/Special:Search")
            or norm_path.startswith("/search/")
            or norm_path.startswith("/wiki/Special:Search/")
        ):
            return "wikipedia_results"
        return "wikipedia_article"

    # GitHub
    if host == "github.com" or host.endswith(".github.com"):
        norm_path = path.rstrip("/") if path != "/" else path
        if norm_path == "/search" or norm_path.startswith("/search/"):
            return "github_results"
        return "github_other"

    return "unknown"


def is_signin_url(url: Any, allow_loopback: bool = False) -> bool:
    """Return True if the URL is an authentication, sign-in, or consent page."""
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
    except Exception:
        return False

    if not host:
        return False

    # Blocked authentication and consent hosts
    if host in ("accounts.google.com", "consent.youtube.com", "consent.google.com"):
        return True

    if (
        host.endswith(".accounts.google.com")
        or host.endswith(".consent.youtube.com")
        or host.endswith(".consent.google.com")
    ):
        return True

    if re.match(
        r"^(accounts|consent)\.(google|youtube)\.(com|co\.[a-z]{2}|com\.[a-z]{2}|[a-z]{2})$",
        host,
        re.IGNORECASE,
    ):
        return True

    # On interactive sites, block sign-in and login paths
    if is_interactive(url, allow_loopback=allow_loopback):
        path_lower = path.lower()
        if (
            path_lower.startswith("/signin")
            or path_lower.startswith("/servicelogin")
            or path.startswith("/ServiceLogin")
            or path_lower.startswith("/login")
        ):
            return True

    return False


def get_site_display_name(url: str) -> str:
    """Extract a friendly display name for a website from its URL."""
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
    except Exception:
        return "The site"

    if "youtube" in host:
        return "YouTube"
    if "google" in host or is_google_host(host):
        return "Google"
    if "github" in host:
        return "GitHub"
    if "wikipedia" in host:
        return "Wikipedia"
    return host or "The site"


def extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from URL query parameters, youtu.be path, or /watch/ path."""
    try:
        p = urlparse(url)
        from urllib.parse import parse_qs
        qs = parse_qs(p.query)
        if "v" in qs and qs["v"]:
            return qs["v"][0]
        if p.hostname in ("youtu.be", "www.youtu.be"):
            return p.path.strip("/")
        if "/watch" in p.path:
            parts = [seg for seg in p.path.split("/") if seg and seg != "watch"]
            if parts:
                return parts[0]
    except Exception:
        pass
    return None


def verify_click_result(
    url_before: str,
    url_after: str,
    allow_loopback: bool = False,
) -> tuple[bool, str]:
    """Verify outcome of click_result.

    Returns (is_valid, reason). reason is '' when valid, or 'signin', 'same_url',
    'wrong_page_type', 'same_video', etc.
    """
    if is_signin_url(url_after, allow_loopback=allow_loopback):
        return False, "signin"

    if url_after == url_before:
        return False, "same_url"

    pt_before = page_type(url_before, allow_loopback=allow_loopback)
    pt_after = page_type(url_after, allow_loopback=allow_loopback)

    # YouTube: youtube_home, youtube_results, youtube_watch
    if pt_before in ("youtube_home", "youtube_results", "youtube_watch"):
        if pt_after not in ("youtube_watch", "youtube_shorts"):
            return False, "wrong_page_type"
        if pt_before == "youtube_watch" and pt_after == "youtube_watch":
            vid_before = extract_video_id(url_before)
            vid_after = extract_video_id(url_after)
            if not vid_after or vid_after == vid_before:
                return False, "same_video"
        return True, ""

    # YouTube Shorts
    if pt_before == "youtube_shorts":
        if pt_after in ("youtube_shorts", "youtube_watch"):
            return True, ""
        return False, "wrong_page_type"

    # Google: google_results, google_other
    if pt_before.startswith("google_"):
        try:
            parsed = urlparse(url_after)
            scheme = (parsed.scheme or "").lower()
            host = (parsed.hostname or "").lower()
            if scheme not in ("http", "https"):
                return False, "non_http"
            if is_google_host(host) or host == "google.com" or host.endswith(".google.com"):
                return False, "still_google"
            return True, ""
        except Exception:
            return False, "invalid_url"

    # Wikipedia
    if pt_before.startswith("wikipedia_"):
        if pt_after != "wikipedia_article":
            return False, "wrong_page_type"
        return True, ""

    # GitHub
    if pt_before.startswith("github_"):
        if pt_after != "github_other":
            return False, "wrong_page_type"
        return True, ""

    # General fallback
    if pt_after == pt_before:
        return False, "wrong_page_type"

    return True, ""

