"""OC destination host teardown: groups, inbounds, ProxyHost, and destination rows."""

from __future__ import annotations

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, ProxyHost, ProxyInbound, inbounds_groups_association
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


def _scope_predicate(model, workspace_id: int | None, tenant_id: int | None):
    """Same resource scope (workspace, else tenant, else global/unscoped)."""
    if workspace_id is not None:
        return model.workspace_id == workspace_id
    if tenant_id is not None:
        return and_(model.workspace_id.is_(None), model.tenant_id == tenant_id)
    return and_(model.workspace_id.is_(None), model.tenant_id.is_(None))


async def orphan_destination_tag_for_host(db: AsyncSession, host: ProxyHost) -> str | None:
    """
    Destination tag for a host whose virtual inbound was lost (``inbound_tag`` is NULL).

    Orphans are matched to the single unbound destination row in the host's workspace with the
    same display name.  Ambiguous matches return ``None`` so unrelated rows are never touched.
    """
    if host.inbound_tag:
        return None
    remark = (host.remark or "").strip()
    if not remark or (host.workspace_id is None and host.tenant_id is None):
        return None
    stmt = (
        select(OCPanelDestinationHost, OCPanel)
        .join(OCPanel, OCPanel.id == OCPanelDestinationHost.panel_id)
        .where(
            OCPanelDestinationHost.display_name == remark,
            OCPanelDestinationHost.source_missing.is_(False),
        )
    )
    if host.workspace_id is not None:
        stmt = stmt.where(OCPanel.workspace_id == host.workspace_id)
    else:
        stmt = stmt.where(OCPanel.tenant_id == host.tenant_id)
    rows = (await db.execute(stmt)).all()
    candidates = [
        effective_destination_inbound_tag(panel.id, dest.destination_config_id, dest.virtual_inbound_tag)
        for dest, panel in rows
    ]
    if not candidates:
        return None
    bound = set(
        (
            await db.execute(select(ProxyHost.inbound_tag).where(ProxyHost.inbound_tag.in_(candidates)))
        ).scalars().all()
    )
    unbound = [tag for tag in candidates if tag not in bound]
    return unbound[0] if len(unbound) == 1 else None


async def detach_unhosted_native_tag(
    db: AsyncSession,
    tag: str,
    *,
    workspace_id: int | None,
    tenant_id: int | None,
) -> int:
    """
    After a native host was deleted: when no host with ``tag`` remains in that scope, remove the
    tag from the scope's groups so groups never keep a membership with nothing behind it.
    Groups and hosts of other workspaces/tenants are not touched.
    """
    if not tag:
        return 0
    remaining = (
        await db.execute(
            select(ProxyHost.id)
            .where(ProxyHost.inbound_tag == tag, _scope_predicate(ProxyHost, workspace_id, tenant_id))
            .limit(1)
        )
    ).first()
    if remaining is not None:
        return 0
    inbound_ids = (await db.execute(select(ProxyInbound.id).where(ProxyInbound.tag == tag))).scalars().all()
    if not inbound_ids:
        return 0
    group_ids = (
        await db.execute(select(Group.id).where(_scope_predicate(Group, workspace_id, tenant_id)))
    ).scalars().all()
    if not group_ids:
        return 0
    result = await db.execute(
        delete(inbounds_groups_association).where(
            inbounds_groups_association.c.inbound_id.in_(inbound_ids),
            inbounds_groups_association.c.group_id.in_(group_ids),
        )
    )
    return int(result.rowcount or 0)


async def delete_hosts_and_detach_membership(db: AsyncSession, hosts: list[ProxyHost]) -> dict[str, int]:
    """
    Delete hosts and keep Group membership consistent (no stale references).

    * OC destination hosts: full teardown (group refs, inbound, destination row, host).
    * Orphaned OC hosts (NULL tag): teardown of the single matching destination row, if any.
    * Native hosts: delete the host; detach its tag from the scope's groups when it was the last
      host with that tag in the scope.

    Does not commit; the caller owns the transaction.
    """
    stats = {"oc_teardown": 0, "orphan_teardown": 0, "native_deleted": 0, "membership_detached": 0}
    native_ids: list[int] = []
    scope_tags: set[tuple[int | None, int | None, str]] = set()

    snapshot = [(h.id, h.inbound_tag, h.workspace_id, h.tenant_id, h) for h in hosts]
    for host_id, tag, workspace_id, tenant_id, host in snapshot:
        if is_oc_destination_inbound_tag(tag) and await teardown_oc_destination_inbound_tag(db, tag or ""):
            stats["oc_teardown"] += 1
            continue
        orphan_tag = await orphan_destination_tag_for_host(db, host)
        if orphan_tag:
            await teardown_oc_destination_inbound_tag(db, orphan_tag)
            stats["orphan_teardown"] += 1
        native_ids.append(host_id)
        if tag:
            scope_tags.add((workspace_id, tenant_id, tag))

    if native_ids:
        await db.execute(delete(ProxyHost).where(ProxyHost.id.in_(native_ids)))
        stats["native_deleted"] = len(native_ids)
    for workspace_id, tenant_id, tag in sorted(scope_tags, key=lambda item: (str(item[0]), str(item[1]), item[2])):
        stats["membership_detached"] += await detach_unhosted_native_tag(
            db, tag, workspace_id=workspace_id, tenant_id=tenant_id
        )
    return stats
