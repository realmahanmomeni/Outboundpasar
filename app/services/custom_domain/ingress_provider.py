"""Ingress and certificate provider selection for custom subscription domains (Phase 3-B.3)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

class IngressProviderKind(StrEnum):
    """Explicit ingress integration; never inferred from stale optional URLs alone."""

    none = "none"
    caddy = "caddy"
    stub = "stub"


@dataclass(frozen=True, slots=True)
class IngressProviderStatus:
    """Safe, operator-facing snapshot of ingress/certificate integration health."""

    provider: IngressProviderKind
    configured: bool
    reachable: bool
    routing_capable: bool
    public_https_ready: bool
    message: str

    @property
    def can_activate_custom_domains(self) -> bool:
        return (
            self.configured
            and self.reachable
            and self.routing_capable
            and self.public_https_ready
        )


def describe_ingress_architecture_options() -> list[dict[str, str]]:
    """Document evaluated ingress options for operators (no live infrastructure calls)."""
    return [
        {
            "id": "dedicated_ip",
            "title": "Dedicated public IP for PasarGuard",
            "summary": (
                "Bind TLS/listener on :443 on a separate IP so Outbound Center :443 is untouched. "
                "Use CUSTOM_DOMAIN_INGRESS_PROVIDER=caddy or another terminator on that IP."
            ),
        },
        {
            "id": "external_tls",
            "title": "External TLS / managed custom hostnames",
            "summary": (
                "Terminate customer TLS at CDN or managed proxy; forward to PasarGuard origin "
                "(for example :2053) with authenticated origin and trusted proxy headers."
            ),
        },
        {
            "id": "shared_ingress_extension",
            "title": "Shared ingress extension (Outbound Center)",
            "summary": (
                "Route selected hostnames to PasarGuard via the existing :443 owner. "
                "Requires separate authorization; not implemented in this repository."
            ),
        },
        {
            "id": "origin_tls_2053",
            "title": "Direct TLS on application port (for example :2053)",
            "summary": (
                "Useful for technical tests only. Standard https://customer.example URLs without "
                "a port still hit :443 and will not reach this listener."
            ),
        },
    ]
