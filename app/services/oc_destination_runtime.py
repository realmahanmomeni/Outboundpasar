"""Resolve OC destination host tags to upstream subscription links."""

from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProxyHost
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelDestinationHost
from app.services.oc_share_link import (
    effective_destination_inbound_tag,
    match_upstream_link_for_oc_source_config,
    parse_destination_tag,
)


async def catalog_source_config_id_for_destination(
    db: AsyncSession,
    panel_id: int,
    destination_config_id: str,
) -> str | None:
    """Resolve catalog config id for a destination fingerprint (when payload omits oc_source_config_id)."""
    dest_id = (destination_config_id or "").strip()
    if not dest_id:
        return None
    row = (
        await db.execute(
            select(OCPanelDestinationHost.source_payload).where(
                OCPanelDestinationHost.panel_id == panel_id,
                OCPanelDestinationHost.destination_config_id == dest_id,
                OCPanelDestinationHost.source_missing.is_(False),
            )
        )
    ).scalar_one_or_none()
    if isinstance(row, dict):
        sid = str(row.get("oc_source_config_id") or "").strip()
        if sid:
            return sid
    legacy = (
        await db.execute(
            select(OCPanelConfig.source_config_id).where(
                OCPanelConfig.panel_id == panel_id,
                OCPanelConfig.source_config_id == dest_id,
                OCPanelConfig.source_missing.is_(False),
            )
        )
    ).scalar_one_or_none()
    return str(legacy) if legacy else None


def destination_config_allowed_by_sync(
    *,
    oc_source_config_id: str,
    panel_id: int,
    destination_config_id: str,
    synced: set[str],
) -> bool:
    """True when a destination host tag should be included for runtime/subscription output."""
    sid = (oc_source_config_id or "").strip()
    dest = (destination_config_id or "").strip()
    if not synced:
        return False
    if sid:
        if sid in synced:
            return True
        return bool(dest and dest in synced)
    return bool(dest and dest in synced)


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

    # One query for every candidate host (was one query per destination row).  Scope rules mirror
    # ``_scope_proxy_host_stmt``; rows nobody matches still fall back to the per-row resolver.
    candidate_tags: set[str] = set()
    for dest in dest_rows:
        if dest.virtual_inbound_tag:
            candidate_tags.add(dest.virtual_inbound_tag)
        key = (dest.panel_id, dest.destination_config_id)
        if key in desired_by_key:
            candidate_tags.add(desired_by_key[key])
    hosts_by_tag: dict[str, list[ProxyHost]] = {}
    if candidate_tags:
        host_rows = (
            await db.execute(
                select(ProxyHost).where(ProxyHost.inbound_tag.in_(candidate_tags)).order_by(ProxyHost.id.asc())
            )
        ).scalars().all()
        for host in host_rows:
            hosts_by_tag.setdefault(host.inbound_tag, []).append(host)

    def _in_scope(host: ProxyHost, panel: OCPanel) -> bool:
        if panel.tenant_id is not None and host.tenant_id != panel.tenant_id:
            return False
        if panel.workspace_id is not None and host.workspace_id != panel.workspace_id:
            return False
        return True

    out: list[tuple[OCPanelDestinationHost, ProxyHost | None]] = []
    for dest in sorted(dest_rows, key=lambda row: (row.panel_id, row.id)):
        panel = panels.get(dest.panel_id)
        if panel is None:
            continue
        key = (dest.panel_id, dest.destination_config_id)
        if key not in desired_by_key:
            continue
        effective_tag = desired_by_key[key]
        proxy: ProxyHost | None = None
        for tag in (dest.virtual_inbound_tag, effective_tag):
            if not tag:
                continue
            proxy = next((h for h in hosts_by_tag.get(tag, []) if _in_scope(h, panel)), None)
            if proxy is not None:
                break
        if proxy is None:
            proxy = await _proxy_host_for_destination(db, panel, dest, effective_tag)
        out.append((dest, proxy))
    # Deterministic output order independent of DB heap order: host priority, then stable ids.
    out.sort(key=lambda pair: (pair[1].priority if pair[1] is not None else 0, pair[0].panel_id, pair[0].id))
    return out
