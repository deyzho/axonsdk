"""Shared security utilities for the Axon SDK."""

from __future__ import annotations

import re
import socket
from urllib.parse import urlparse

from axon.exceptions import AxonError


# Regex that matches private/loopback/unroutable IP ranges in URLs.
# Covers:
#   - localhost
#   - 127.x.x.x       (IPv4 loopback)
#   - 10.x.x.x         (RFC-1918 class A)
#   - 172.16-31.x.x    (RFC-1918 class B)
#   - 192.168.x.x      (RFC-1918 class C)
#   - 169.254.x.x      (link-local / AWS EC2 IMDS / Azure IMDS / GCP metadata)
#   - 0.0.0.0          (unspecified address)
#   - ::1              (IPv6 loopback, bare)
#   - [::1]            (IPv6 loopback, bracket notation)
#   - [fe80::...]      (IPv6 link-local, bracket notation)
_PRIVATE_IP_RE = re.compile(
    r"^https?://"
    r"(localhost"
    r"|127\."
    r"|10\."
    r"|172\.(1[6-9]|2[0-9]|3[01])\."
    r"|192\.168\."
    r"|169\.254\."
    r"|0\.0\.0\.0"
    r"|::1"
    r"|\[::1\]"
    r"|\[fe80"
    r")"
)

# Regex for raw IPv4 addresses (used in resolved-IP validation)
_IPV4_PRIVATE_RE = re.compile(
    r"^(127\."
    r"|10\."
    r"|172\.(1[6-9]|2[0-9]|3[01])\."
    r"|192\.168\."
    r"|169\.254\."
    r"|0\.0\.0\.0"
    r")"
)


def assert_safe_url(url: str, provider: str, label: str = "URL") -> None:
    """
    Raise AxonError if *url* is not safe to contact.

    Rules enforced:
    1. Must use HTTPS (http:// is rejected).
    2. Must not contain a private/loopback/link-local/metadata IP in the URL itself.
    3. DNS rebinding defence: resolves the hostname to an IP and re-validates
       against the private-range blocklist (best-effort; never blocks on failure).

    Blocked ranges:
    - RFC-1918 private (10.x, 172.16-31.x, 192.168.x)
    - IPv4 loopback (127.x) and unspecified (0.0.0.0)
    - IPv4 link-local / cloud metadata service (169.254.x) — covers AWS EC2 IMDS,
      Azure IMDS, and GCP instance metadata endpoints
    - IPv6 loopback (::1) and link-local (fe80::/10)

    Args:
        url: The URL to validate.
        provider: Provider name used in the error message.
        label: Human-readable label for the URL (e.g. "IPFS URL", "worker endpoint").

    Raises:
        AxonError: If the URL fails any safety check.
    """
    if not url.startswith("https://"):
        raise AxonError(f"[{provider}] {label} must use HTTPS.")

    if _PRIVATE_IP_RE.match(url):
        raise AxonError(f"[{provider}] {label} must not point to a private/local address.")

    # DNS rebinding defence: resolve the hostname and re-check the resulting IP.
    # Failures (DNS errors, timeouts) are silently ignored so a transient DNS
    # outage never blocks legitimate requests.
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        # Only attempt resolution for hostname strings, not raw IP literals
        if host and not _looks_like_ip(host):
            resolved = socket.gethostbyname(host)
            if _IPV4_PRIVATE_RE.match(resolved):
                raise AxonError(
                    f"[{provider}] {label} hostname '{host}' resolves to a "
                    f"private/local address ({resolved})."
                )
    except AxonError:
        raise
    except Exception:
        pass  # DNS resolution is best-effort


def _looks_like_ip(host: str) -> bool:
    """Return True if *host* is already a raw IPv4 or IPv6 literal."""
    # IPv6 in URLs comes wrapped in brackets (e.g. [::1]); strip them
    if host.startswith("[") and host.endswith("]"):
        return True
    try:
        socket.inet_pton(socket.AF_INET, host)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return True
    except OSError:
        pass
    return False
