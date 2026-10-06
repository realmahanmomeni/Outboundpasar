"""Host options for Group assignment (display names, inbound tags internally)."""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProxyHost
from app.db.models_oc import OCPanel, OCPanelDestinationHost
from app.services.oc_share_link import effective_destination_inbound_tag


class GroupHostOption(BaseModel):
    """One selectable Host for Group create/edit (persisted as group.inbound_tags)."""

    inbound_tag: str
    display_name: str
    host_id: int | None = None
    destination_config_id: str | None = None
    panel_id: int | None = None
    is_oc_destination: bool = False


async def list_group_host_options(
    db: AsyncSession,
    *,
    tenant_id: int | None = None,
    workspace_id: int | None = None,
) -> list[GroupHostOption]:
    """
    Selectable Hosts for Group create/edit.

    Imported OC panels: one option per destination subscription config (``OCPanelDestinationHost``).
    Native panels: manually configured ``ProxyHost`` rows (non ``oc_`` tags).

    Orphan core/xray inbound names are never listed by themselves; only resources backed by
    a destination row or a workspace ``ProxyHost`` appear.

    Legacy OC catalog virtual inbounds (``oc_<panel>_<catalog_id>``) are never group options.
    """
    by_tag: dict[str, GroupHostOption] = {}

    dest_stmt = (
        select(OCPanelDestinationHost, OCPanel.id)
        .join(OCPanel, OCPanel.id == OCPanelDestinationHost.panel_id)
        .where(
            OCPanelDestinationHost.source_missing.is_(False),
            OCPanelDestinationHost.locally_hidden.is_(False),
        )
    )
    if tenant_id is not None:
        dest_stmt = dest_stmt.where(OCPanel.tenant_id == tenant_id)
    if workspace_id is not None:
        dest_stmt = dest_stmt.where(OCPanel.workspace_id == workspace_id)

    dest_pairs = (await db.execute(dest_stmt)).all()

    dest_rows: list[tuple[OCPanelDestinationHost, int]] = [(row, panel_id) for row, panel_id in dest_pairs]
    dest_tags = [
        effective_destination_inbound_tag(panel_id, row.destination_config_id, row.virtual_inbound_tag)
        for row, panel_id in dest_rows
    ]
    display_names = {row.display_name for row, _ in dest_rows if row.display_name}

    host_id_by_tag: dict[str, int] = {}
    host_id_by_remark: dict[str, int] = {}
    if dest_tags or display_names:
        host_stmt = select(ProxyHost)
        if dest_tags and display_names:
            host_stmt = host_stmt.where(
                or_(
                    ProxyHost.inbound_tag.in_(dest_tags),
                    and_(ProxyHost.inbound_tag.is_(None), ProxyHost.remark.in_(display_names)),
                )
            )
        elif dest_tags:
            host_stmt = host_stmt.where(ProxyHost.inbound_tag.in_(dest_tags))
        if tenant_id is not None:
            host_stmt = host_stmt.where(ProxyHost.tenant_id == tenant_id)
        if workspace_id is not None:
            host_stmt = host_stmt.where(ProxyHost.workspace_id == workspace_id)
        for host in (await db.execute(host_stmt)).scalars().all():
            if host.id is None:
                continue
            if host.inbound_tag:
                host_id_by_tag[host.inbound_tag] = host.id
            remark = (host.remark or "").strip()
            if remark and host.inbound_tag is None and remark in display_names:
                host_id_by_remark[remark] = host.id

    for row, panel_id in dest_rows:
        tag = effective_destination_inbound_tag(panel_id, row.destination_config_id, row.virtual_inbound_tag)
        label = (row.display_name or "").strip() or tag
        by_tag[tag] = GroupHostOption(
            inbound_tag=tag,
            display_name=label,
            host_id=host_id_by_tag.get(tag) or host_id_by_remark.get(label),
            destination_config_id=row.destination_config_id,
            panel_id=row.panel_id,
            is_oc_destination=True,
        )

    native_stmt = select(ProxyHost).where(~ProxyHost.inbound_tag.like("oc_%"))
    if tenant_id is not None:
        native_stmt = native_stmt.where(ProxyHost.tenant_id == tenant_id)
    if workspace_id is not None:
        native_stmt = native_stmt.where(ProxyHost.workspace_id == workspace_id)
    native_hosts = (await db.execute(native_stmt)).scalars().all()
    for host in native_hosts:
        tag = host.inbound_tag
        if not tag:
            continue
        label = (host.remark or "").strip() or tag
        by_tag.setdefault(
            tag,
            GroupHostOption(
                inbound_tag=tag,
                display_name=label,
                host_id=host.id,
            ),
        )

    return sorted(by_tag.values(), key=lambda item: (item.display_name.lower(), item.inbound_tag))


async def allowed_group_inbound_tags(
    db: AsyncSession,
    *,
    tenant_id: int | None = None,
    workspace_id: int | None = None,
) -> set[str]:
    """Tags valid for ``Group.inbound_tags`` (same inventory as the Group host picker)."""
    return {
        opt.inbound_tag
        for opt in await list_group_host_options(db, tenant_id=tenant_id, workspace_id=workspace_id)
    }
