"""Resolve OC destination host tags to upstream subscription links."""

from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProxyHost
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelDestinationHost
from app.services.oc_share_link import (
    effective_destination_inbound_tag,
    match_links_to_configs,
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
        unmatched_discovery_links: list[str] = []
        for row in rows:
            payload = row.source_payload if isinstance(row.source_payload, dict) else {}
            sid = str(payload.get("oc_source_config_id") or "").strip()
            if sid:
                config_ids.add(sid)
                continue
            raw = str(payload.get("subscription_link") or "").strip()
            if raw:
                unmatched_discovery_links.append(raw)

        if unmatched_discovery_links:
            catalog_configs = (
                await db.execute(
                    select(OCPanelConfig).where(
                        OCPanelConfig.panel_id == panel_id,
                        OCPanelConfig.source_missing.is_(False),
                    )
                )
            ).scalars().all()
            if catalog_configs:
                pairs = [(c.source_config_id, c.source_name) for c in catalog_configs]
                matched = match_links_to_configs(unmatched_discovery_links, pairs)
                config_ids.update(matched.keys())

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


def _desired_tags_by_destination_key(oc_tags: list[str]) -> dict[tuple[int, str], str]:
    out: dict[tuple[int, str], str] = {}
    for tag in oc_tags:
        parsed = parse_destination_tag(tag)
        if parsed is not None:
            out[parsed] = tag
    return out


def _scope_proxy_host_stmt(stmt, panel: OCPanel):
    if panel.tenant_id is not None:
        stmt = stmt.where(ProxyHost.tenant_id == panel.tenant_id)
    if panel.workspace_id is not None:
        stmt = stmt.where(ProxyHost.workspace_id == panel.workspace_id)
    return stmt


async def _first_proxy_host(db: AsyncSession, stmt) -> ProxyHost | None:
    return (await db.execute(stmt.order_by(ProxyHost.id.asc()).limit(1))).scalars().first()


async def _proxy_host_for_destination(
    db: AsyncSession,
    panel: OCPanel,
    dest: OCPanelDestinationHost,
    effective_tag: str,
) -> ProxyHost | None:
    if dest.virtual_inbound_tag:
        stmt = _scope_proxy_host_stmt(
            select(ProxyHost).where(ProxyHost.inbound_tag == dest.virtual_inbound_tag),
            panel,
        )
        host = await _first_proxy_host(db, stmt)
        if host is not None:
            return host
    stmt = _scope_proxy_host_stmt(
        select(ProxyHost).where(ProxyHost.inbound_tag == effective_tag),
        panel,
    )
    host = await _first_proxy_host(db, stmt)
    if host is not None:
        return host
    remark = (dest.display_name or "").strip()
    if not remark:
        return None
    stmt = select(ProxyHost).where(
        ProxyHost.inbound_tag.is_(None),
        ProxyHost.remark == remark,
        or_(ProxyHost.is_disabled.is_(False), ProxyHost.is_disabled.is_(None)),
    )
    stmt = _scope_proxy_host_stmt(stmt, panel)
    return await _first_proxy_host(db, stmt)


async def load_destination_hosts_for_tags(
    db: AsyncSession,
    oc_tags: list[str],
    panel_ids: list[int],
) -> list[tuple[OCPanelDestinationHost, ProxyHost | None]]:
    if not oc_tags or not panel_ids:
        return []
    desired_by_key = _desired_tags_by_destination_key(oc_tags)
    if not desired_by_key:
        return []

    key_clauses = [
        and_(
            OCPanelDestinationHost.panel_id == pid,
            OCPanelDestinationHost.destination_config_id == did,
        )
        for pid, did in desired_by_key
        if pid in panel_ids
    ]
    if not key_clauses:
        return []

    tag_clause = OCPanelDestinationHost.virtual_inbound_tag.in_(oc_tags)
    identity_clause = or_(tag_clause, or_(*key_clauses))

    dest_rows = (
        await db.execute(
            select(OCPanelDestinationHost)
            .where(
                OCPanelDestinationHost.panel_id.in_(panel_ids),
                identity_clause,
                OCPanelDestinationHost.source_missing.is_(False),
                OCPanelDestinationHost.locally_hidden.is_(False),
            )
        )
    ).scalars().all()

    panels = {
        p.id: p
        for p in (await db.execute(select(OCPanel).where(OCPanel.id.in_(panel_ids)))).scalars().all()
    }

    out: list[tuple[OCPanelDestinationHost, ProxyHost | None]] = []
    for dest in dest_rows:
        panel = panels.get(dest.panel_id)
        if panel is None:
            continue
        key = (dest.panel_id, dest.destination_config_id)
        if key not in desired_by_key:
            continue
        effective_tag = desired_by_key[key]
        proxy = await _proxy_host_for_destination(db, panel, dest, effective_tag)
        out.append((dest, proxy))
    return out
