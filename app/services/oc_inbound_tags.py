"""Inbound tag helpers for Groups (aligned with destination OC Host inventory)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.manager import core_manager
from app.services.assignable_hosts import allowed_group_inbound_tags


async def list_assignable_inbound_tags(db: AsyncSession) -> list[str]:
    """
    Inbound tags for native Host create/edit (core Xray inbounds only).

    OC virtual tags are not user-selectable parent inbounds; destination OC Hosts
    are materialized from Outbound Center subscription sync.
    """
    core = await core_manager.get_inbounds()
    return sorted({str(tag) for tag in core if tag and not str(tag).startswith("oc_")})


def _is_legacy_oc_catalog_tag(tag: str) -> bool:
    from app.services.oc_share_link import is_oc_destination_inbound_tag

    return tag.startswith("oc_") and not is_oc_destination_inbound_tag(tag)


async def validate_inbound_tags_for_group(
    db: AsyncSession,
    tags: list[str],
    *,
    tenant_id: int | None = None,
    workspace_id: int | None = None,
    raise_error,
) -> None:
    if not tags:
        return
    legacy = [t for t in tags if _is_legacy_oc_catalog_tag(t)]
    if legacy:
        await raise_error(
            "Legacy OC catalog inbound tags cannot be assigned to groups; "
            "select destination hosts from group-host-options instead.",
            400,
        )
    allowed = await allowed_group_inbound_tags(db, tenant_id=tenant_id, workspace_id=workspace_id)
    for tag in tags:
        if tag not in allowed:
            await raise_error(f"{tag} not found", 400)

