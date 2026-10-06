"""OC panel/config filtering for subscription and runtime output."""

from __future__ import annotations

import hashlib

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelDestinationHost, OCUserMapping
from app.node.oc_sync import OC_PANEL_BLOCKING_STATUSES
from app.services.oc_connection_credentials import panel_has_active_connection
from app.services.oc_user_mapping_state import oc_user_mapping_runtime_active_criteria


def parse_oc_inbound_panel_id(tag: str) -> int | None:
    if not tag.startswith("oc_"):
        return None
    parts = tag.split("_")
    if len(parts) >= 3 and parts[1].isdigit():
        panel_id = int(parts[1])
        return panel_id if panel_id > 0 else None
    return None


async def _panel_allows_oc_runtime_read(
    db: AsyncSession,
    panel: OCPanel,
    *,
    user_tenant_id: int | None,
    user_workspace_id: int | None,
) -> bool:
    if panel.tenant_id is None:
        return True
    if user_tenant_id is not None and panel.tenant_id != user_tenant_id:
        return False
    if (
        panel.workspace_id is not None
        and user_workspace_id is not None
        and panel.workspace_id != user_workspace_id
    ):
        return False
    return await panel_has_active_connection(db, panel)


async def load_runtime_oc_subscription_state(
    db: AsyncSession,
    user_id: int,
    user_tenant_id: int | None,
    *,
    user_workspace_id: int | None = None,
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

    if user_workspace_id is None:
        user_workspace_id = await db.scalar(select(User.workspace_id).where(User.id == user_id))

    panel_ids = {m.panel_id for m in mappings}
    panels_by_id = {
        p.id: p
        for p in (
            await db.execute(select(OCPanel).where(OCPanel.id.in_(panel_ids)))
        ).scalars().all()
    }

    allowed_panels: set[int] = set()
    synced_by_panel: dict[int, set[str]] = {}
    for mapping in mappings:
        panel = panels_by_id.get(mapping.panel_id)
        if panel is None:
            continue
        if (panel.sync_status or "") in OC_PANEL_BLOCKING_STATUSES:
            continue
        if not await _panel_allows_oc_runtime_read(
            db, panel, user_tenant_id=user_tenant_id, user_workspace_id=user_workspace_id
        ):
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

    from app.services.oc_share_link import parse_destination_tag

    allowed_panels, synced_by_panel = await load_runtime_oc_subscription_state(db, user_id, user_tenant_id)
    if not allowed_panels:
        return []

    dest_tags = [t for t in desired_oc_tags if parse_destination_tag(t)]
    legacy_tags = [t for t in desired_oc_tags if t not in dest_tags]

    allowed_tag_set: set[str] = set()

    if dest_tags:
        dest_keys = [p for t in dest_tags if (p := parse_destination_tag(t))]
        key_clauses = [
            and_(
                OCPanelDestinationHost.panel_id == pid,
                OCPanelDestinationHost.destination_config_id == did,
            )
            for pid, did in dest_keys
        ]
        tag_clause = OCPanelDestinationHost.virtual_inbound_tag.in_(dest_tags)
        identity_clause = or_(tag_clause, or_(*key_clauses)) if key_clauses else tag_clause
        dest_rows = (
            await db.execute(
                select(
                    OCPanelDestinationHost.virtual_inbound_tag,
                    OCPanelDestinationHost.panel_id,
                    OCPanelDestinationHost.destination_config_id,
                    OCPanelDestinationHost.source_payload,
                ).where(
                    identity_clause,
                    OCPanelDestinationHost.source_missing.is_(False),
                )
            )
        ).all()
        desired_by_key = {p: t for t in dest_tags for p in [parse_destination_tag(t)] if p}
        for _virt_tag, panel_id, dest_id, source_payload in dest_rows:
            if panel_id not in allowed_panels:
                continue
            tag = desired_by_key.get((panel_id, dest_id)) or _virt_tag
            if not tag:
                continue
            payload = source_payload if isinstance(source_payload, dict) else {}
            oc_sid = str(payload.get("oc_source_config_id") or "").strip()
            synced = synced_by_panel.get(panel_id, set())
            if oc_sid and synced and oc_sid not in synced:
                continue
            allowed_tag_set.add(tag)

    if legacy_tags:
        config_rows = (
            await db.execute(
                select(
                    OCPanelConfig.virtual_inbound_tag,
                    OCPanelConfig.panel_id,
                    OCPanelConfig.source_config_id,
                ).where(OCPanelConfig.virtual_inbound_tag.in_(legacy_tags))
            )
        ).all()
        for tag, panel_id, source_config_id in config_rows:
            if panel_id not in allowed_panels:
                continue
            if source_config_id not in synced_by_panel.get(panel_id, set()):
                continue
            allowed_tag_set.add(tag)

    return [tag for tag in desired_oc_tags if tag in allowed_tag_set]


async def runtime_oc_subscription_cache_fingerprint(
    db: AsyncSession, user_id: int, user_tenant_id: int | None
) -> tuple[tuple[int, tuple[str, ...], str | None], ...]:
    """Stable cache key component reflecting runtime-active OC panel/config state."""
    allowed_panels, synced_by_panel = await load_runtime_oc_subscription_state(db, user_id, user_tenant_id)
    if not allowed_panels:
        return ()
    url_fingerprints: dict[int, str | None] = {}
    for panel_id, external_url in (
        await db.execute(
            select(OCUserMapping.panel_id, OCUserMapping.external_subscription_url).where(
                OCUserMapping.user_id == user_id,
                OCUserMapping.panel_id.in_(allowed_panels),
                oc_user_mapping_runtime_active_criteria(),
            )
        )
    ).all():
        raw = (external_url or "").strip()
        url_fingerprints[panel_id] = (
            hashlib.sha256(raw.encode()).hexdigest()[:16] if raw else None
        )
    return tuple(
        sorted(
            (
                panel_id,
                tuple(sorted(synced_by_panel.get(panel_id, set()))),
                url_fingerprints.get(panel_id),
            )
            for panel_id in allowed_panels
        )
    )


async def resolve_user_tenant_id(db: AsyncSession, user_id: int, admin_id: int | None) -> int | None:
    from app.db.models import Admin, User

    if admin_id is None:
        admin_id = await db.scalar(select(User.admin_id).where(User.id == user_id))
    if admin_id is None:
        return None
    return await db.scalar(select(Admin.tenant_id).where(Admin.id == admin_id))
