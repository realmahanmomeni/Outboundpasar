"""Runtime wiring for custom-domain DNS and certificate integrations."""
from __future__ import annotations

from app import on_startup
from app.services.custom_domain.caddy_provisioner import CaddyCertificateProvisioner
from app.services.custom_domain.dns import set_dns_txt_resolver
from app.services.custom_domain.dns_resolver import build_production_dns_txt_resolver
from app.services.custom_domain.ssl_lifecycle import set_certificate_provisioner
from config import custom_domain_settings, runtime_settings


@on_startup
def configure_custom_domain_integrations() -> None:
    if runtime_settings.testing:
        return
    if custom_domain_settings.dns_enabled:
        set_dns_txt_resolver(
            build_production_dns_txt_resolver(
                custom_domain_settings.dns_timeout_seconds,
                custom_domain_settings.dns_nameservers,
            )
        )
    admin_url = custom_domain_settings.caddy_admin_url.strip()
    if admin_url:
        set_certificate_provisioner(
            CaddyCertificateProvisioner(
                admin_url,
                upstream_dial=custom_domain_settings.caddy_upstream_dial,
            )
        )
