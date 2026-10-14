"""DNS TXT verification for custom domains (injectable for tests)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.services.custom_domain.dns_errors import DnsResolutionError
from app.services.custom_domain.normalization import verification_txt_record_name, verification_txt_record_value


class DnsTxtResolver(Protocol):
    async def lookup_txt(self, name: str) -> list[str]: ...


_dns_resolver: DnsTxtResolver | None = None


def set_dns_txt_resolver(resolver: DnsTxtResolver | None) -> None:
    global _dns_resolver
    _dns_resolver = resolver


def get_dns_txt_resolver() -> DnsTxtResolver | None:
    return _dns_resolver


def _configured_production_resolver() -> DnsTxtResolver | None:
    from config import custom_domain_settings, runtime_settings

    if runtime_settings.testing or not custom_domain_settings.dns_enabled:
        return None
    from app.services.custom_domain.dns_resolver import build_production_dns_txt_resolver

    return build_production_dns_txt_resolver(
        custom_domain_settings.dns_timeout_seconds,
        custom_domain_settings.dns_nameservers,
    )


async def lookup_txt_records(name: str) -> list[str]:
    resolver = _dns_resolver or _configured_production_resolver()
    if resolver is None:
        return []
    return await resolver.lookup_txt(name)


def _normalize_txt_values(values: list[str]) -> set[str]:
    normalized: set[str] = set()
    for value in values:
        stripped = value.strip()
        if stripped.startswith('"') and stripped.endswith('"') and len(stripped) >= 2:
            stripped = stripped[1:-1]
        normalized.add(stripped)
    return normalized


async def dns_verification_matches(hostname_normalized: str, token: str) -> bool:
    record_name = verification_txt_record_name(hostname_normalized)
    expected = verification_txt_record_value(token)
    try:
        values = await lookup_txt_records(record_name)
    except DnsResolutionError:
        return False
    return expected in _normalize_txt_values(values)
