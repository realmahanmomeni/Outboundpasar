"""Runtime wiring for custom-domain DNS and certificate integrations."""
from __future__ import annotations

from app import on_startup
from app.services.custom_domain.caddy_provisioner import CaddyCertificateProvisioner
from app.services.custom_domain.dns import set_dns_txt_resolver
from app.services.custom_domain.dns_resolver import build_production_dns_txt_resolver
from app.services.custom_domain.ingress_provider import IngressProviderKind
from app.services.custom_domain.ssl_lifecycle import (
    StubCertificateProvisioner,
    UnconfiguredCertificateProvisioner,
    set_certificate_provisioner,
)
from app.utils.logger import get_logger
from config import custom_domain_settings, runtime_settings

logger = get_logger("custom-domain-bootstrap")


def _resolve_ingress_provider_kind() -> IngressProviderKind:
    raw = (custom_domain_settings.ingress_provider or "none").strip().lower()
    try:
        return IngressProviderKind(raw)
    except ValueError:
        logger.warning("Unknown CUSTOM_DOMAIN_INGRESS_PROVIDER=%s; using none", raw)
        return IngressProviderKind.none


@on_startup
def configure_custom_domain_integrations() -> None:
    if runtime_settings.testing:
        set_certificate_provisioner(StubCertificateProvisioner())
        return

    if custom_domain_settings.dns_enabled:
        set_dns_txt_resolver(
            build_production_dns_txt_resolver(
                custom_domain_settings.dns_timeout_seconds,
                custom_domain_settings.dns_nameservers,
            )
        )

    kind = _resolve_ingress_provider_kind()
    if kind == IngressProviderKind.caddy:
        admin_url = custom_domain_settings.caddy_admin_url.strip()
        if not admin_url:
            logger.warning(
                "CUSTOM_DOMAIN_INGRESS_PROVIDER=caddy but CUSTOM_DOMAIN_CADDY_ADMIN_URL is empty; "
                "custom domains will remain inactive"
            )
            set_certificate_provisioner(UnconfiguredCertificateProvisioner())
            return
        set_certificate_provisioner(
            CaddyCertificateProvisioner(
                admin_url,
                upstream_dial=custom_domain_settings.caddy_upstream_dial,
            )
        )
        return

    if kind == IngressProviderKind.stub:
        logger.warning("CUSTOM_DOMAIN_INGRESS_PROVIDER=stub is ignored outside TESTING")
        set_certificate_provisioner(UnconfiguredCertificateProvisioner())
        return

    set_certificate_provisioner(UnconfiguredCertificateProvisioner())
