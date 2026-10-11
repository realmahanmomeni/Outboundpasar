"""Resolve per-user OC subscription links for authorized virtual inbound tags."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProxyHost
from app.db.models_oc import OCPanelConfig, OCUserMapping
from app.services.oc_destination_runtime import (
    load_destination_hosts_for_tags,
    match_upstream_link_for_destination,
)
from app.services.oc_host_display import resolve_oc_subscription_remark_source
from app.services.oc_share_link import (
    decode_subscription_links,
    index_subscription_links,
    match_link_for_config,
    parse_destination_tag,
    parse_share_link,
    replace_link_remark,
    share_link_config_base,
    share_link_structural_key,
)
from app.services.oc_subscription_runtime import load_runtime_oc_subscription_state
from app.services.oc_upstream_subscription import fetch_upstream_subscription_body, validate_upstream_subscription_url
from app.services.oc_user_mapping_state import oc_user_mapping_runtime_active_criteria
from app.utils.logger import get_logger

logger = get_logger("oc-user-sub-hosts")


async def _fetch_user_links(mapping: OCUserMapping) -> list[str] | None:
    url = (mapping.external_subscription_url or "").strip()
    if not url:
        return None
    try:
        validate_upstream_subscription_url(url)
    except ValueError:
        return None
    try:
        body = await fetch_upstream_subscription_body(url, "links")
    except Exception as exc:
        logger.warning(
            "oc_user_sub_fetch_failed user=%s panel=%s error=%s",
            mapping.user_id,
            mapping.panel_id,
            type(exc).__name__,
        )
        return None
    raw_links = decode_subscription_links(body if isinstance(body, str) else body.decode())
    return [
        link
        for link in raw_links
        if link.startswith(("vless://", "vmess://", "trojan://", "ss://", "hysteria2://", "wireguard://"))
    ]


def _structural_index(panel_links: list[str]) -> dict[str, list[str]]:
    """Endpoint identity (credential/remark independent) -> distinct user links."""
    index: dict[str, list[str]] = {}
    for link in panel_links:
        key = share_link_structural_key(link)
        if key:
            bucket = index.setdefault(key, [])
            # Same credential + endpoint under different remarks is one config.
            if all(share_link_config_base(existing) != share_link_config_base(link) for existing in bucket):
                bucket.append(link)
    return index


def match_user_link_by_endpoint(
    structural: dict[str, list[str]],
    stored_discovery_link: str,
) -> str | None:
    """
    Stable match of a destination host to the customer's own link: same endpoint, any remark.

    Remark-based matching (kept as fallback) cannot distinguish stylized Unicode remarks, which
    normalize to an empty key, nor remarks that are identical across different endpoints.
    """
    key = share_link_structural_key(stored_discovery_link)
    if not key:
        return None
    candidates = structural.get(key) or []
    return candidates[0] if len(candidates) == 1 else None


async def collect_oc_subscription_links_for_user(
    db: AsyncSession,
    user_id: int,
    oc_tags: list[str],
    format_variables: dict,
    *,
    user_tenant_id: int | None,
) -> list[str]:
    """
    Return share links (remark templated) for OC virtual tags the user may use.
    Destination host tags match upstream configs by stable OC catalog config id, not discovery URI fingerprint.
    """
    if not oc_tags:
        return []

    allowed_panels, synced_by_panel = await load_runtime_oc_subscription_state(db, user_id, user_tenant_id)
    if not allowed_panels:
        return []

    mappings = {
        row.panel_id: row
        for row in (
            await db.execute(
                select(OCUserMapping).where(
                    OCUserMapping.user_id == user_id,
                    OCUserMapping.panel_id.in_(allowed_panels),
                    oc_user_mapping_runtime_active_criteria(),
                )
            )
        ).scalars().all()
    }

    out: list[str] = []
    seen: set[str] = set()
    stages: Counter[str] = Counter()
    stages["selected_tags"] = len(oc_tags)

    dest_tags = [t for t in oc_tags if parse_destination_tag(t)]
    legacy_tags = [t for t in oc_tags if t not in dest_tags]

    catalog_names: dict[tuple[int, str], str] = {}
    if dest_tags:
        sid_rows = (
            await db.execute(
                select(OCPanelConfig.panel_id, OCPanelConfig.source_config_id, OCPanelConfig.source_name).where(
                    OCPanelConfig.panel_id.in_(allowed_panels),
                    OCPanelConfig.source_missing.is_(False),
                )
            )
        ).all()
        for panel_id, sid, sname in sid_rows:
            catalog_names[(panel_id, sid)] = sname

    if dest_tags:
        desired_by_key = {
            parsed: tag
            for tag in dest_tags
            for parsed in [parse_destination_tag(tag)]
            if parsed is not None
        }
        dest_rows = await load_destination_hosts_for_tags(db, dest_tags, list(allowed_panels))
        links_by_panel: dict[int, list[str]] = {}
        structural_by_panel: dict[int, dict[str, list[str]]] = {}
        stages["destination_rows"] = len(dest_rows)
        stages["dest_tags_without_row"] = max(0, len(dest_tags) - len(dest_rows))
        for dest_host, _proxy in dest_rows:
            tag = desired_by_key.get((dest_host.panel_id, dest_host.destination_config_id))
            if not tag or tag not in oc_tags:
                stages["rejected_tag_not_selected"] += 1
                continue
            mapping = mappings.get(dest_host.panel_id)
            if mapping is None:
                stages["rejected_no_active_mapping"] += 1
                continue
            synced = synced_by_panel.get(dest_host.panel_id, set())
            payload = dest_host.source_payload if isinstance(dest_host.source_payload, dict) else {}
            oc_sid = str(payload.get("oc_source_config_id") or "").strip()
            from app.services.oc_destination_runtime import destination_config_allowed_by_sync

            if synced and not destination_config_allowed_by_sync(
                oc_source_config_id=oc_sid,
                panel_id=dest_host.panel_id,
                destination_config_id=dest_host.destination_config_id,
                synced=synced,
            ):
                stages["rejected_not_in_synced_configs"] += 1
                continue
            if dest_host.panel_id not in links_by_panel:
                user_links = await _fetch_user_links(mapping)
                links_by_panel[dest_host.panel_id] = user_links or []
                structural_by_panel[dest_host.panel_id] = _structural_index(user_links or [])
            panel_links = links_by_panel[dest_host.panel_id]
            if not panel_links:
                stages["rejected_no_user_links"] += 1
            raw: str | None = None
            stored_discovery = str(payload.get("subscription_link") or "").strip()
            if panel_links and stored_discovery:
                raw = match_user_link_by_endpoint(structural_by_panel[dest_host.panel_id], stored_discovery)
                if raw:
                    stages["matched_by_endpoint"] += 1
            if raw:
                pass
            elif oc_sid and panel_links:
                source_name = catalog_names.get((dest_host.panel_id, oc_sid), oc_sid)
                raw = match_upstream_link_for_destination(
                    panel_links,
                    oc_sid,
                    source_name=source_name,
                )
            elif panel_links:
                index = index_subscription_links(panel_links)
                parsed_match = match_link_for_config(
                    index,
                    source_config_id=dest_host.display_name,
                    source_name=dest_host.display_name,
                )
                raw = parsed_match.raw if parsed_match else None
                if not raw:
                    stored = str(payload.get("subscription_link") or "").strip()
                    disc = parse_share_link(stored) if stored else None
                    if disc is not None:
                        parsed_match = match_link_for_config(
                            index,
                            source_config_id=disc.remark or dest_host.display_name,
                            source_name=dest_host.display_name,
                        )
                        raw = parsed_match.raw if parsed_match else None
            if not raw:
                if panel_links:
                    stages["rejected_no_matching_user_link"] += 1
                continue
            remark_source = resolve_oc_subscription_remark_source(
                display_name=dest_host.display_name,
                display_name_template=dest_host.display_name_template,
            )
            try:
                remark = remark_source.format_map(format_variables)
            except Exception:
                remark = remark_source
            link = replace_link_remark(raw, remark)
            if link in seen:
                stages["duplicates_removed"] += 1
                continue
            seen.add(link)
            out.append(link)

    if legacy_tags:
        logger.warning(
            "oc_user_sub_legacy_tags user=%s tags=%s count=%s",
            user_id,
            legacy_tags[:8],
            len(legacy_tags),
        )
        config_rows = (
            await db.execute(
                select(OCPanelConfig, ProxyHost)
                .join(ProxyHost, ProxyHost.inbound_tag == OCPanelConfig.virtual_inbound_tag)
                .where(
                    OCPanelConfig.virtual_inbound_tag.in_(legacy_tags),
                    OCPanelConfig.panel_id.in_(allowed_panels),
                    OCPanelConfig.source_missing.is_(False),
                    OCPanelConfig.locally_hidden.is_(False),
                    or_(ProxyHost.is_disabled.is_(False), ProxyHost.is_disabled.is_(None)),
                )
            )
        ).all()
        for config, host in config_rows:
            tag = config.virtual_inbound_tag
            if not tag or tag not in legacy_tags:
                continue
            if config.source_config_id not in synced_by_panel.get(config.panel_id, set()):
                continue
            mapping = mappings.get(config.panel_id)
            if mapping is None:
                continue
            user_links = await _fetch_user_links(mapping)
            if not user_links:
                continue
            parsed = match_link_for_config(
                index_subscription_links(user_links),
                source_config_id=config.source_config_id,
                source_name=config.source_name,
            )
            if parsed is None:
                continue
            remark_source = resolve_oc_subscription_remark_source(
                display_name=host.remark,
                display_name_template=config.display_name_template,
                fallback=parsed.remark,
            )
            try:
                remark = remark_source.format_map(format_variables)
            except Exception:
                remark = remark_source
            link = replace_link_remark(parsed.raw, remark)
            if link in seen:
                continue
            seen.add(link)
            out.append(link)

    logger.info(
        "oc_user_sub_collected user=%s desired_dest=%s desired_legacy=%s rendered=%s panels=%s stages=%s",
        user_id,
        len(dest_tags),
        len(legacy_tags),
        len(out),
        sorted(allowed_panels),
        dict(sorted(stages.items())),
    )
    return out
