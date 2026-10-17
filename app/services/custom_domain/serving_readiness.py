"""Separate ownership linkage from public HTTPS serving readiness."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.custom_domain import get_managed_domain_for_workspace
from app.db.crud.managed_subdomain_binding import get_claimed_binding_for_workspace, get_pending_binding_for_workspace_admin
from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState
from app.services.custom_domain.ssl_lifecycle import domain_is_routable_for_subscriptions
from config import custom_domain_settings


async def managed_subdomain_serving_ready(
    db: AsyncSession,
    domain: SubscriptionCustomDomain | None,
) -> bool:
    if domain is None:
        return False
    if domain.state not in {
        SubscriptionCustomDomainState.active,
    }:
        return False
    return await domain_is_routable_for_subscriptions(
        domain,
        public_https_ready=custom_domain_settings.ingress_public_https_ready,
    )


async def workspace_managed_subdomain_status(
    db: AsyncSession,
    *,
    workspace_id: int,
    binding_admin_id: int,
) -> tuple[bool, bool, str | None, str | None]:
    """
    Returns (ownership_linked, serving_ready, label, fqdn).
    URL generation must use serving_ready only.
    """
    domain = await get_managed_domain_for_workspace(db, workspace_id)
    pending = await get_pending_binding_for_workspace_admin(
        db, workspace_id=workspace_id, binding_admin_id=binding_admin_id
    )
    claimed = await get_claimed_binding_for_workspace(db, workspace_id)
    ownership_linked = (
        pending is not None
        or claimed is not None
        or (
            domain is not None
            and domain.state
            not in {SubscriptionCustomDomainState.disabled, SubscriptionCustomDomainState.failed}
        )
    )
    serving_ready = await managed_subdomain_serving_ready(db, domain)
    label = None
    fqdn = None
    if domain is not None and domain.oc_subdomain_label:
        label = domain.oc_subdomain_label
        fqdn = domain.hostname_normalized
    elif serving_ready and domain is not None:
        label = domain.hostname_normalized.split(".", 1)[0]
        fqdn = domain.hostname_normalized
    return ownership_linked, serving_ready, label, fqdn
