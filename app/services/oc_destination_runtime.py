"""Resolve OC destination host tags to upstream subscription links."""

from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProxyHost
from app.db.models_oc import OCPanelConfig, OCPanelDestinationHost
from app.services.oc_share_link import (
    match_upstream_link_for_oc_source_config,
    parse_destination_tag,
)


async def source_config_ids_for_panel_tags(
    db: AsyncSession,
    panel_id: int,
    tags: list[str],
) -> list[str]:
    """Map group inbound tags to OC catalog config ids for Outbound Center user PUT."""
    if not tags:
        return []
    dest_tags = [t for t in tags if parse_destination_tag(t)]
    legacy_tags = [t for t in tags if t not in dest_tags]

    config_ids: set[str] = set()

    if dest_tags:
        dest_keys = [p for t in dest_tags if (p := parse_destination_tag(t))]
        key_clauses = [
            and_(
                OCPanelDestinationHost.panel_id == pid,
                OCPanelDestinationHost.destination_config_id == did,
            )
            for pid, did in dest_keys
            if pid == panel_id
        ]
        tag_clause = OCPanelDestinationHost.virtual_inbound_tag.in_(dest_tags)
        identity_clause = or_(tag_clause, or_(*key_clauses)) if key_clauses else tag_clause
        rows = (
            await db.execute(
                select(OCPanelDestinationHost).where(
                    OCPanelDestinationHost.panel_id == panel_id,
                    identity_clause,
                    OCPanelDestinationHost.source_missing.is_(False),
                )
            )
        ).scalars().all()
        for row in rows:
            payload = row.source_payload if isinstance(row.source_payload, dict) else {}
            sid = str(payload.get("oc_source_config_id") or "").strip()
            if sid:
                config_ids.add(sid)

    if legacy_tags:
        legacy = (
            await db.execute(
                select(OCPanelConfig.source_config_id).where(
                    OCPanelConfig.panel_id == panel_id,
                    OCPanelConfig.virtual_inbound_tag.in_(legacy_tags),
                    OCPanelConfig.source_missing.is_(False),
                )
            )
        ).scalars().all()
        config_ids.update(legacy)

    return sorted(config_ids)


def match_upstream_link_for_destination(
    raw_links: list[str],
    oc_source_config_id: str,
    *,
    source_name: str | None = None,
) -> str | None:
    """Match customer upstream link by stable OC catalog config id (not discovery URI fingerprint)."""
    return match_upstream_link_for_oc_source_config(
        raw_links,
        oc_source_config_id,
        source_name=source_name,
    )


async def load_destination_hosts_for_tags(
    db: AsyncSession,
    oc_tags: list[str],
    panel_ids: list[int],
) -> list[tuple[OCPanelDestinationHost, ProxyHost]]:
    if not oc_tags:
        return []
    parsed_tags = [(t, parse_destination_tag(t)) for t in oc_tags]
    dest_keys = {p for _, p in parsed_tags if p}
    if not dest_keys:
        return []
    key_clauses = [
        and_(
            OCPanelDestinationHost.panel_id == pid,
            OCPanelDestinationHost.destination_config_id == did,
        )
        for pid, did in dest_keys
    ]
    tag_clause = OCPanelDestinationHost.virtual_inbound_tag.in_(oc_tags)
    identity_clause = or_(tag_clause, or_(*key_clauses))

    rows = (
        await db.execute(
            select(OCPanelDestinationHost, ProxyHost)
            .join(ProxyHost, ProxyHost.inbound_tag == OCPanelDestinationHost.virtual_inbound_tag)
            .where(
                OCPanelDestinationHost.panel_id.in_(panel_ids),
                identity_clause,
                OCPanelDestinationHost.source_missing.is_(False),
                OCPanelDestinationHost.locally_hidden.is_(False),
                or_(ProxyHost.is_disabled.is_(False), ProxyHost.is_disabled.is_(None)),
            )
        )
    ).all()
    return list(rows)
