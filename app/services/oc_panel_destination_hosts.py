"""Materialize one local OC Host per destination subscription config."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProxyHost, ProxyInbound
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelDestinationHost
from app.services.oc_host_display import DEFAULT_OC_HOST_DISPLAY_TEMPLATE, remark_looks_like_user_template
from app.services.oc_share_link import (
    ParsedShareLink,
    destination_virtual_inbound_tag,
    match_links_to_configs,
    subscription_config_links,
    subscription_link_uri_fingerprint,
)


def _apply_parsed_link_to_host(host: ProxyHost, parsed: ParsedShareLink) -> None:
    if parsed.address:
        host.address = {parsed.address}
    if parsed.port:
        host.port = parsed.port
    if parsed.path:
        host.path = parsed.path
    if parsed.sni:
        host.sni = set(parsed.sni)
    if parsed.host:
        host.host = set(parsed.host)


def _build_payload(parsed: ParsedShareLink, oc_source_config_id: str | None) -> dict:
    return {
        "subscription_link": parsed.raw,
        "subscription_parsed": {
            "protocol": parsed.protocol,
            "address": parsed.address,
            "port": parsed.port,
            "network": parsed.network,
            "remark": parsed.remark,
        },
        "destination_config_id": subscription_link_uri_fingerprint(parsed.raw),
        "oc_source_config_id": oc_source_config_id,
    }


def _apply_panel_scope_to_host(host: ProxyHost, panel: OCPanel) -> None:
    if panel.tenant_id is not None and host.tenant_id is None:
        host.tenant_id = panel.tenant_id
    if panel.workspace_id is not None:
        host.workspace_id = panel.workspace_id


async def _ensure_inbound_host(
    db: AsyncSession,
    *,
    tag: str,
    display_name: str,
    panel: OCPanel | None = None,
) -> tuple[ProxyHost, bool]:
    inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one_or_none()
    if inbound is None:
        inbound = ProxyInbound(tag=tag)
        db.add(inbound)
        await db.flush()

    host = (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == tag))).scalar_one_or_none()
    created = False
    if host is None:
        host = ProxyHost(
            remark=display_name,
            priority=0,
            address=set(),
            port=None,
            path=None,
            allowinsecure=None,
            alpn=[],
            status=[],
        )
        host.inbound = inbound
        db.add(host)
        created = True
    else:
        if remark_looks_like_user_template(host.remark) or not (host.remark or "").strip():
            host.remark = display_name
    if panel is not None:
        _apply_panel_scope_to_host(host, panel)
    return host, created


async def ensure_panel_destination_hosts_materialized(
    db: AsyncSession,
    panel: OCPanel,
) -> int:
    """
    Idempotently restore ``ProxyInbound``/``ProxyHost`` and ``virtual_inbound_tag`` for
    existing destination rows (e.g. after inbound deletion left FKs NULL).
    """
    from app.services.oc_share_link import parse_share_link

    rows = (
        await db.execute(
            select(OCPanelDestinationHost).where(
                OCPanelDestinationHost.panel_id == panel.id,
                OCPanelDestinationHost.source_missing.is_(False),
            )
        )
    ).scalars().all()
    for row in rows:
        tag = destination_virtual_inbound_tag(panel.id, row.destination_config_id)
        row.virtual_inbound_tag = tag
        payload = row.source_payload if isinstance(row.source_payload, dict) else {}
        raw = str(payload.get("subscription_link") or "").strip()
        parsed = parse_share_link(raw) if raw else None
        display_name = row.display_name or (parsed.remark if parsed else row.destination_config_id)
        host, _ = await _ensure_inbound_host(db, tag=tag, display_name=display_name, panel=panel)
        host.is_disabled = False
        if parsed is not None:
            _apply_parsed_link_to_host(host, parsed)
    if rows:
        await db.flush()
        return len(rows)


async def reconcile_panel_destination_hosts(
    db: AsyncSession,
    panel: OCPanel,
    raw_links: list[str],
) -> int:
    """
    Create/update one ``OCPanelDestinationHost`` (+ ProxyHost) per destination subscription config.
    """
    parsed_links = subscription_config_links(raw_links)
    if not parsed_links:
        return 0

    catalog_configs = (
        await db.execute(
            select(OCPanelConfig).where(
                OCPanelConfig.panel_id == panel.id,
                OCPanelConfig.source_missing.is_(False),
            )
        )
    ).scalars().all()
    config_pairs = [(c.source_config_id, c.source_name) for c in catalog_configs]
    matched_catalog = match_links_to_configs(raw_links, config_pairs)
    raw_to_catalog = {p.raw: cid for cid, p in matched_catalog.items()}

    existing_rows = (
        await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
    ).scalars().all()
    by_dest_id = {r.destination_config_id: r for r in existing_rows}

    seen: set[str] = set()
    updated = 0

    for parsed in parsed_links:
        dest_id = subscription_link_uri_fingerprint(parsed.raw)
        if not dest_id:
            continue
        seen.add(dest_id)
        display_name = (parsed.remark or "").strip() or dest_id
        tag = destination_virtual_inbound_tag(panel.id, dest_id)
        oc_source = raw_to_catalog.get(parsed.raw)

        row = by_dest_id.get(dest_id)
        if row is None:
            host, _ = await _ensure_inbound_host(db, tag=tag, display_name=display_name, panel=panel)
            row = OCPanelDestinationHost(
                panel_id=panel.id,
                destination_config_id=dest_id,
                display_name=display_name,
                virtual_inbound_tag=tag,
                source_payload=_build_payload(parsed, oc_source),
                display_name_template=DEFAULT_OC_HOST_DISPLAY_TEMPLATE,
                source_missing=False,
            )
            db.add(row)
            _apply_parsed_link_to_host(host, parsed)
            updated += 1
        else:
            row.display_name = display_name
            row.source_missing = False
            row.locally_hidden = False
            row.source_payload = _build_payload(parsed, oc_source)
            if not row.display_name_template:
                row.display_name_template = DEFAULT_OC_HOST_DISPLAY_TEMPLATE
            row.virtual_inbound_tag = tag
            host, _ = await _ensure_inbound_host(db, tag=tag, display_name=display_name, panel=panel)
            host.remark = display_name
            host.is_disabled = False
            _apply_parsed_link_to_host(host, parsed)
            updated += 1

    for row in existing_rows:
        if row.destination_config_id in seen:
            continue
        row.source_missing = True
        if row.virtual_inbound_tag:
            host = (
                await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == row.virtual_inbound_tag))
            ).scalar_one_or_none()
            if host is not None:
                host.is_disabled = True

    for cfg in catalog_configs:
        cfg.locally_hidden = True
        if cfg.virtual_inbound_tag:
            legacy = (
                await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == cfg.virtual_inbound_tag))
            ).scalar_one_or_none()
            if legacy is not None:
                legacy.is_disabled = True

    await ensure_panel_destination_hosts_materialized(db, panel)
    await db.flush()
    return updated


async def update_oc_destination_host_display_name(
    db: AsyncSession,
    host: ProxyHost,
    display_name: str,
) -> None:
    """Keep ``OCPanelDestinationHost.display_name`` in sync with ``ProxyHost.remark``."""
    from app.services.oc_share_link import parse_destination_tag

    parsed = parse_destination_tag(host.inbound_tag or "")
    if not parsed:
        return
    panel_id, dest_id = parsed
    row = (
        await db.execute(
            select(OCPanelDestinationHost).where(
                OCPanelDestinationHost.panel_id == panel_id,
                OCPanelDestinationHost.destination_config_id == dest_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    name = display_name.strip()
    row.display_name = name
    host.remark = name
