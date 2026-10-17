"""Production DNS TXT resolver using dnspython (injectable default for non-test runtimes)."""
from __future__ import annotations

import asyncio
import re
from functools import lru_cache

import dns.exception
import dns.name
import dns.resolver
import dns.rdatatype

from app.services.custom_domain.dns_errors import (
    DnsMalformedResponse,
    DnsNameNotAllowed,
    DnsNoData,
    DnsNxDomain,
    DnsResolutionError,
    DnsServFail,
    DnsTimeout,
)
from app.services.custom_domain.normalization import verification_txt_record_name

_VERIFICATION_NAME_RE = re.compile(
    r"^_pasarguard-verification\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def _assert_safe_txt_query_name(name: str) -> None:
    candidate = name.strip().rstrip(".").lower()
    if not candidate or len(candidate) > 253:
        raise DnsNameNotAllowed("invalid query name length")
    if not _VERIFICATION_NAME_RE.match(candidate):
        raise DnsNameNotAllowed("query name is not a custom-domain verification record")
    # Derive hostname and ensure it matches the canonical verification record name.
    hostname = candidate.removeprefix("_pasarguard-verification.")
    if verification_txt_record_name(hostname) != candidate:
        raise DnsNameNotAllowed("query name does not match verification record convention")


def _decode_txt_rdata(rdata) -> str:
    try:
        if getattr(rdata, "strings", None):
            joined = b"".join(rdata.strings)
            return joined.decode("utf-8", errors="replace")
        text = rdata.to_text()
        if text.startswith('"') and text.endswith('"'):
            return text[1:-1]
        return text
    except Exception as exc:
        raise DnsMalformedResponse("malformed TXT rdata") from exc


class ProductionDnsTxtResolver:
    """
    Resolve TXT records via the system recursive resolver (or configured nameservers).

    Caching: dnspython's resolver may cache internally; this class does not add an
    application-level cache. Operators should expect DNS propagation delay (minutes to hours)
    after publishing TXT records before verification succeeds.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        nameservers: list[str] | None = None,
    ) -> None:
        self._timeout_seconds = max(1.0, min(timeout_seconds, 30.0))
        self._nameservers = nameservers

    def _lookup_sync(self, name: str) -> list[str]:
        _assert_safe_txt_query_name(name)
        resolver = dns.resolver.Resolver(configure=True)
        resolver.lifetime = self._timeout_seconds
        resolver.timeout = self._timeout_seconds
        if self._nameservers:
            resolver.nameservers = self._nameservers
        try:
            answers = resolver.resolve(name, dns.rdatatype.TXT)
        except dns.resolver.NXDOMAIN as exc:
            raise DnsNxDomain(str(exc)) from exc
        except dns.resolver.NoAnswer as exc:
            raise DnsNoData(str(exc)) from exc
        except dns.resolver.NoNameservers as exc:
            raise DnsServFail(str(exc)) from exc
        except dns.exception.Timeout as exc:
            raise DnsTimeout(str(exc)) from exc
        except dns.resolver.LifetimeTimeout as exc:
            raise DnsTimeout(str(exc)) from exc
        except dns.exception.DNSException as exc:
            raise DnsResolutionError(str(exc)) from exc

        values: list[str] = []
        for rdata in answers:
            values.append(_decode_txt_rdata(rdata))
        return values

    async def lookup_txt(self, name: str) -> list[str]:
        return await asyncio.to_thread(self._lookup_sync, name)


@lru_cache(maxsize=1)
def build_production_dns_txt_resolver(timeout_seconds: float, nameservers_key: str) -> ProductionDnsTxtResolver:
    nameservers = [ns.strip() for ns in nameservers_key.split(",") if ns.strip()] or None
    return ProductionDnsTxtResolver(timeout_seconds=timeout_seconds, nameservers=nameservers)
