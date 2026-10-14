"""DNS TXT verification for custom domains (injectable for tests)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.services.custom_domain.normalization import verification_txt_record_name, verification_txt_record_value


class DnsTxtResolver(Protocol):
    async def lookup_txt(self, name: str) -> list[str]: ...


_dns_resolver: DnsTxtResolver | None = None


def set_dns_txt_resolver(resolver: DnsTxtResolver | None) -> None:
    global _dns_resolver
    _dns_resolver = resolver


async def lookup_txt_records(name: str) -> list[str]:
    if _dns_resolver is None:
        return []
    return await _dns_resolver.lookup_txt(name)


async def dns_verification_matches(hostname_normalized: str, token: str) -> bool:
    record_name = verification_txt_record_name(hostname_normalized)
    expected = verification_txt_record_value(token)
    values = await lookup_txt_records(record_name)
    normalized = {v.strip().strip('"') for v in values}
    return expected in normalized
