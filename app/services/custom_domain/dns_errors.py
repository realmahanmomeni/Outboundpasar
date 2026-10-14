"""DNS TXT lookup errors for custom-domain verification (fail-closed semantics)."""
from __future__ import annotations


class DnsResolutionError(Exception):
    """Base class for resolver failures that must not count as successful verification."""


class DnsNxDomain(DnsResolutionError):
    pass


class DnsNoData(DnsResolutionError):
    pass


class DnsTimeout(DnsResolutionError):
    pass


class DnsServFail(DnsResolutionError):
    pass


class DnsMalformedResponse(DnsResolutionError):
    pass


class DnsNameNotAllowed(DnsResolutionError):
    """Query name failed local safety checks before any network I/O."""
