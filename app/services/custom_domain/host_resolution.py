"""Hostname-to-workspace resolution for subscription custom domains."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState
from app.services.custom_domain.normalization import normalize_custom_domain_hostname


async def get_domain_by_hostname(db: AsyncSession, hostname: str) -> SubscriptionCustomDomain | None:
    try:
        normalized = normalize_custom_domain_hostname(hostname)
    except Exception:
        return None
    return (
        await db.execute(
            select(SubscriptionCustomDomain).where(SubscriptionCustomDomain.hostname_normalized == normalized)
        )
    ).scalar_one_or_none()


async def get_active_subscription_domain(
    db: AsyncSession, hostname: str
) -> SubscriptionCustomDomain | None:
    domain = await get_domain_by_hostname(db, hostname)
    if domain is None or domain.state != SubscriptionCustomDomainState.active:
        return None
    return domain
