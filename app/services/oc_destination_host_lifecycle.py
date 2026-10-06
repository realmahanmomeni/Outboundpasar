"""OC destination host teardown: groups, inbounds, ProxyHost, and destination rows."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProxyHost, ProxyInbound, inbounds_groups_association
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelDestinationHost
from app.services.oc_share_link import (
    destination_virtual_inbound_tag,
    effective_destination_inbound_tag,
    is_oc_destination_inbound_tag,
    parse_destination_tag,
)


async def detach_inbound_tag_from_groups(db: AsyncSession, inbound_tag: str) -> int:
    """Remove all group associations for this inbound tag. Returns rows removed."""
    inbound_ids = (
        await db.execute(select(ProxyInbound.id).where(ProxyInbound.tag == inbound_tag))
    ).scalars().all()
    if not inbound_ids:
        return 0
    result = await db.execute(
        delete(inbounds_groups_association).where(
            inbounds_groups_association.c.inbound_id.in_(inbound_ids)
        )
    )
    return int(result.rowcount or 0)


async def delete_inbound_tag_runtime(db: AsyncSession, inbound_tag: str) -> None:
    """
    Remove a virtual inbound from groups and delete its ProxyHost + ProxyInbound.

    ProxyInbound ``after_delete`` also clears group associations; we detach first so
    groups never retain stale inbound ids if inbound delete is skipped.
    """
    tag = (inbound_tag or "").strip()
    if not tag:
        return
    await detach_inbound_tag_from_groups(db, tag)
    await db.execute(delete(ProxyHost).where(ProxyHost.inbound_tag == tag))
    await db.execute(delete(ProxyInbound).where(ProxyInbound.tag == tag))


async def delete_oc_destination_host_row(
    db: AsyncSession,
    panel_id: int,
    destination_config_id: str,
) -> None:
    await db.execute(
        delete(OCPanelDestinationHost).where(
            OCPanelDestinationHost.panel_id == panel_id,
            OCPanelDestinationHost.destination_config_id == destination_config_id,
        )
    )


async def teardown_oc_destination_inbound(
    db: AsyncSession,
    panel_id: int,
    destination_config_id: str,
    *,
    virtual_inbound_tag: str | None = None,
    delete_destination_row: bool = True,
) -> str:
    """
    Full teardown for one OC destination host (manual delete or panel cleanup).

    Returns the inbound tag that was removed.
    """
    tag = effective_destination_inbound_tag(panel_id, destination_config_id, virtual_inbound_tag)
    if delete_destination_row:
        await delete_oc_destination_host_row(db, panel_id, destination_config_id)
    await delete_inbound_tag_runtime(db, tag)
    return tag


async def teardown_oc_destination_inbound_tag(db: AsyncSession, inbound_tag: str) -> bool:
    """Teardown from a group/subscription inbound tag. Returns False if not an OC destination tag."""
    parsed = parse_destination_tag(inbound_tag)
    if parsed is None:
        return False
    panel_id, dest_id = parsed
    await teardown_oc_destination_inbound(
        db,
        panel_id,
        dest_id,
        virtual_inbound_tag=inbound_tag,
        delete_destination_row=True,
    )
    return True


async def collect_panel_virtual_inbound_tags(db: AsyncSession, panel: OCPanel) -> list[str]:
    """All virtual inbound tags owned by this panel (catalog + destination hosts)."""
    catalog_tags = (
        await db.execute(
            select(OCPanelConfig.virtual_inbound_tag).where(OCPanelConfig.panel_id == panel.id)
        )
    ).scalars().all()
    dest_rows = (
        await db.execute(
            select(
                OCPanelDestinationHost.virtual_inbound_tag,
                OCPanelDestinationHost.destination_config_id,
            ).where(OCPanelDestinationHost.panel_id == panel.id)
        )
    ).all()
    dest_tags: list[str] = []
    for virt_tag, dest_id in dest_rows:
        if virt_tag:
            dest_tags.append(virt_tag)
        elif dest_id:
            dest_tags.append(destination_virtual_inbound_tag(panel.id, dest_id))
    tags = [t for t in (*catalog_tags, *dest_tags) if t]
    return sorted(set(tags))


async def teardown_imported_panel_local_resources(db: AsyncSession, panel: OCPanel) -> list[str]:
    """
    Remove group references, ProxyHosts, and ProxyInbounds for this panel's OC virtual tags.

    Does not enqueue remote OC deletes or remove the OCPanel row.
    """
    tags = await collect_panel_virtual_inbound_tags(db, panel)
    await db.execute(delete(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
    for tag in tags:
        await delete_inbound_tag_runtime(db, tag)
    return tags


def inbound_tag_is_oc_destination(tag: str | None) -> bool:
    return is_oc_destination_inbound_tag(tag)
