"""OC panel/config filtering for subscription and runtime output."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel, OCPanelConfig, OCUserMapping
from app.services.oc_telegram_connection import get_active_connection
from app.services.oc_user_mapping_state import oc_user_mapping_runtime_active_criteria


def parse_oc_inbound_panel_id(tag: str) -> int | None:
    if not tag.startswith("oc_"):
        return None
    parts = tag.split("_")
    if len(parts) >= 3 and parts[1].isdigit():
        panel_id = int(parts[1])
        return panel_id if panel_id > 0 else None
    return None


async def _tenant_allows_oc_runtime_read(
    db: AsyncSession, panel_tenant_id: int | None, user_tenant_id: int | None
) -> bool:
    if panel_tenant_id is None:
        return True
    if user_tenant_id is not None and panel_tenant_id != user_tenant_id:
        return False
    return await get_active_connection(db, panel_tenant_id) is not None


async def load_runtime_oc_subscription_state(
    db: AsyncSession, user_id: int, user_tenant_id: int | None
) -> tuple[set[int], dict[int, set[str]]]:
    """
    Return panel IDs and per-panel synced config IDs eligible for subscription output.
    """
    mappings = (
        await db.execute(
            select(OCUserMapping).where(
                OCUserMapping.user_id == user_id,
                oc_user_mapping_runtime_active_criteria(),
            )
        )
    ).scalars().all()
    if not mappings:
        return set(), {}

    panel_ids = {m.panel_id for m in mappings}
    panel_tenants = {
        row.id: row.tenant_id
        for row in (
            await db.execute(select(OCPanel.id, OCPanel.tenant_id).where(OCPanel.id.in_(panel_ids)))
        ).all()
    }

    allowed_panels: set[int] = set()
    synced_by_panel: dict[int, set[str]] = {}
    for mapping in mappings:
        panel_tenant = panel_tenants.get(mapping.panel_id)
        if not await _tenant_allows_oc_runtime_read(db, panel_tenant, user_tenant_id):
            continue
        allowed_panels.add(mapping.panel_id)
        synced_by_panel[mapping.panel_id] = set(mapping.last_synced_configs or [])

    return allowed_panels, synced_by_panel


async def filter_oc_inbound_tags_for_runtime_subscription(
    db: AsyncSession,
    user_id: int,
    desired_oc_tags: list[str],
    user_tenant_id: int | None,
) -> list[str]:
    """
    Keep only OC virtual inbound tags that are desired (group) and runtime-provisioned.
    """
    if not desired_oc_tags:
        return []

    allowed_panels, synced_by_panel = await load_runtime_oc_subscription_state(db, user_id, user_tenant_id)
    if not allowed_panels:
        return []

    config_rows = (
        await db.execute(
            select(
                OCPanelConfig.virtual_inbound_tag,
                OCPanelConfig.panel_id,
                OCPanelConfig.source_config_id,
            ).where(OCPanelConfig.virtual_inbound_tag.in_(desired_oc_tags))
        )
    ).all()

    allowed_tag_set: set[str] = set()
    for tag, panel_id, source_config_id in config_rows:
        if panel_id not in allowed_panels:
            continue
        if source_config_id not in synced_by_panel.get(panel_id, set()):
            continue
        allowed_tag_set.add(tag)

    return [tag for tag in desired_oc_tags if tag in allowed_tag_set]


async def runtime_oc_subscription_cache_fingerprint(
    db: AsyncSession, user_id: int, user_tenant_id: int | None
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Stable cache key component reflecting runtime-active OC panel/config state."""
    allowed_panels, synced_by_panel = await load_runtime_oc_subscription_state(db, user_id, user_tenant_id)
    if not allowed_panels:
        return ()
    return tuple(
        sorted((panel_id, tuple(sorted(synced_by_panel.get(panel_id, set())))) for panel_id in allowed_panels)
    )


async def resolve_user_tenant_id(db: AsyncSession, user_id: int, admin_id: int | None) -> int | None:
    from app.db.models import Admin, User

    if admin_id is None:
        admin_id = await db.scalar(select(User.admin_id).where(User.id == user_id))
    if admin_id is None:
        return None
    return await db.scalar(select(Admin.tenant_id).where(Admin.id == admin_id))
