"""Shared security utilities for the Axon SDK."""

from __future__ import annotations

import re

from axon.exceptions import AxonError


# Regex that matches private/loopback/unroutable IP ranges in URLs.
# Covers:
#   - localhost
#   - 127.x.x.x  (IPv4 loopback)
#   - 10.x.x.x   (RFC-1918 class A)
#   - 172.16-31.x.x (RFC-1918 class B)
#   - 192.168.x.x (RFC-1918 class C)
#   - 0.0.0.0     (unspecified address)
#   - ::1          (IPv6 loopback, bare)
#   - [::1]        (IPv6 loopback in bracket notation used in URLs)
_PRIVATE_IP_RE = re.compile(
    r"^https?://"
    r"(localhost"
    r"|127\."
    r"|10\."
    r"|172\.(1[6-9]|2[0-9]|3[01])\."
    r"|192\.168\."
    r"|0\.0\.0\.0"
    r"|::1"
    r"|\[::1\]"
    r")"
)


def assert_safe_url(url: str, provider: str, label: str = "URL") -> None:
    """
    Raise AxonError if *url* is not safe to contact.

    Rules enforced:
    - Must use HTTPS (http:// is rejected).
    - Must not resolve to a private/loopback/unroutable address.

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
