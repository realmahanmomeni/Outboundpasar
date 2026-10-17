"""Authorization helpers for edge TLS permission (Caddy on-demand ask) and routing."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState
from app.services.custom_domain.host_resolution import get_domain_by_hostname
from app.services.custom_domain.normalization import normalize_custom_domain_hostname


async def hostname_may_receive_tls_certificate(db: AsyncSession, raw_hostname: str) -> bool:
    """
    Return True only when the hostname is registered, ownership was verified, and issuance is expected.

    Used by the internal Caddy ask endpoint so unknown hosts cannot trigger ACME issuance.
    """
    try:
        normalized = normalize_custom_domain_hostname(raw_hostname)
    except Exception:
        return False
    domain = await get_domain_by_hostname(db, normalized)
    if domain is None:
        return False
    if domain.state == SubscriptionCustomDomainState.disabled:
        return False
    if domain.verified_at is None:
        return False
    return domain.state in {
        SubscriptionCustomDomainState.verified,
        SubscriptionCustomDomainState.pending_certificate,
        SubscriptionCustomDomainState.active,
        SubscriptionCustomDomainState.renewal_pending,
    }


async def active_subscription_domain_for_request(
    db: AsyncSession, hostname: str
) -> SubscriptionCustomDomain | None:
    from app.services.custom_domain.ssl_lifecycle import domain_is_routable_for_subscriptions
    from config import custom_domain_settings

    domain = await get_domain_by_hostname(db, hostname)
    if domain is None:
        return None
    if not await domain_is_routable_for_subscriptions(
        domain,
        public_https_ready=custom_domain_settings.ingress_public_https_ready,
    ):
        return None
    return domain
