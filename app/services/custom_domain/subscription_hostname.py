"""Resolve subscription URL host prefix for a workspace."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState, SubscriptionDomainKind
from app.services.custom_domain.ssl_lifecycle import domain_is_routable_for_subscriptions
from config import custom_domain_settings


async def active_managed_subscription_hostname(
    db: AsyncSession,
    workspace_id: int,
) -> str | None:
    row = (
        await db.execute(
            select(SubscriptionCustomDomain)
            .where(
                SubscriptionCustomDomain.workspace_id == workspace_id,
                SubscriptionCustomDomain.domain_kind == SubscriptionDomainKind.oc_managed.value,
            )
            .order_by(SubscriptionCustomDomain.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if not await domain_is_routable_for_subscriptions(
        row,
        public_https_ready=custom_domain_settings.ingress_public_https_ready,
    ):
        return None
    return row.hostname_normalized


async def subscription_url_prefix_for_workspace(
    db: AsyncSession,
    workspace_id: int,
    *,
    default_prefix: str,
) -> str:
    managed_host = await active_managed_subscription_hostname(db, workspace_id)
    if managed_host:
        return f"https://{managed_host}"
    return default_prefix
