"""Host options for Group assignment (display names, inbound tags internally)."""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.manager import core_manager
from app.db.models import ProxyHost
from app.db.models_oc import OCPanel, OCPanelDestinationHost


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
    Native panels: core inbounds and manually configured ``ProxyHost`` rows (non ``oc_`` tags).

    Legacy OC catalog virtual inbounds (``oc_<panel>_<catalog_id>``) are never group options.
    """
    by_tag: dict[str, GroupHostOption] = {}

    dest_stmt = (
        select(OCPanelDestinationHost)
        .join(OCPanel, OCPanel.id == OCPanelDestinationHost.panel_id)
        .where(
            OCPanelDestinationHost.source_missing.is_(False),
            OCPanelDestinationHost.locally_hidden.is_(False),
            OCPanelDestinationHost.virtual_inbound_tag.isnot(None),
        )
    )
    if tenant_id is not None:
        dest_stmt = dest_stmt.where(OCPanel.tenant_id == tenant_id)
    if workspace_id is not None:
        dest_stmt = dest_stmt.where(OCPanel.workspace_id == workspace_id)

    dest_rows = (await db.execute(dest_stmt)).scalars().all()

    dest_tags = [r.virtual_inbound_tag for r in dest_rows if r.virtual_inbound_tag]
    host_id_by_tag: dict[str, int] = {}
    if dest_tags:
        host_stmt = select(ProxyHost).where(ProxyHost.inbound_tag.in_(dest_tags))
        if tenant_id is not None:
            host_stmt = host_stmt.where(ProxyHost.tenant_id == tenant_id)
        if workspace_id is not None:
            host_stmt = host_stmt.where(ProxyHost.workspace_id == workspace_id)
        for host in (await db.execute(host_stmt)).scalars().all():
            if host.inbound_tag and host.id is not None:
                host_id_by_tag[host.inbound_tag] = host.id

    for row in dest_rows:
        tag = row.virtual_inbound_tag
        if not tag:
            continue
        by_tag[tag] = GroupHostOption(
            inbound_tag=tag,
            display_name=row.display_name,
            host_id=host_id_by_tag.get(tag),
            destination_config_id=row.destination_config_id,
            panel_id=row.panel_id,
            is_oc_destination=True,
        )

    for tag in await core_manager.get_inbounds():
        if not tag or str(tag).startswith("oc_"):
            continue
        key = str(tag)
        by_tag.setdefault(
            key,
            GroupHostOption(inbound_tag=key, display_name=key),
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
