"""OC panel catalog metadata (OCPanelConfig) from Outbound Center.

Catalog rows hold config ids, group mapping, and OC sync metadata only. Selectable
hosts and ProxyHost rows are created exclusively via discovery subscription
reconciliation (``OCPanelDestinationHost``).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProxyHost
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelGroup
from app.services.oc_host_display import DEFAULT_OC_HOST_DISPLAY_TEMPLATE


def virtual_inbound_tag(panel_id: int, source_config_id: str) -> str:
    """Legacy catalog tag pattern (not used for new selectable hosts)."""
    return f"oc_{panel_id}_{source_config_id}"


def oc_config_import_eligible(mapping: dict[str, Any] | None, selected_group_ids: list[str]) -> bool:
    """
    Whether a config from OC should be imported during wizard sync.

    - ``supported=False``: panel-wide config (always import).
    - ``supported=True`` with empty ``groups``: OC lists no mapping; treat as panel-wide.
    - Otherwise require overlap with the operator's selected OC groups.
    """
    mapping = mapping or {}
    if not mapping.get("supported", False):
        return True
    mapped = [str(g) for g in (mapping.get("groups") or [])]
    if not mapped:
        return True
    selected = {str(g) for g in selected_group_ids}
    return any(g in selected for g in mapped)


def resolve_panel_group_id(
    mapping: dict[str, Any] | None,
    existing_group_by_source: dict[str, OCPanelGroup],
) -> int | None:
    mapping = mapping or {}
    if not mapping.get("supported", False):
        return None
    for g_id in [str(g) for g in (mapping.get("groups") or [])]:
        row = existing_group_by_source.get(g_id)
        if row is not None:
            return row.id
    return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _retire_legacy_catalog_proxy_host(db: AsyncSession, tag: str | None) -> None:
    """Disable legacy catalog ``ProxyHost`` rows; they are not group-selectable inventory."""
    if not tag:
        return
    host = (
        await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == tag))
    ).scalar_one_or_none()
    if host is not None:
        host.is_disabled = True


async def upsert_oc_panel_config_row(
    db: AsyncSession,
    *,
    panel: OCPanel,
    c_data: dict[str, Any],
    existing_configs: dict[str, OCPanelConfig],
    existing_group_by_source: dict[str, OCPanelGroup],
) -> tuple[OCPanelConfig, int, int]:
    """Create or update one ``OCPanelConfig`` (metadata only; no catalog ProxyHost)."""
    mapping = c_data.get("group_mapping", {}) or {}
    c_id = str(c_data["id"])
    c_name = str(c_data["name"])
    local_group_id = resolve_panel_group_id(mapping, existing_group_by_source)
    legacy_tag = virtual_inbound_tag(panel.id, c_id)

    configs_created = 0
    hosts_created = 0

    protocol = _optional_str(c_data.get("protocol"))
    network = _optional_str(c_data.get("network"))
    port = _optional_int(c_data.get("port"))

    if c_id not in existing_configs:
        pc = OCPanelConfig(
            panel_id=panel.id,
            source_config_id=c_id,
            source_name=c_name,
            panel_group_id=local_group_id,
            protocol=protocol,
            network=network,
            port=port,
            source_payload=c_data,
            virtual_inbound_tag=None,
            display_name_template=DEFAULT_OC_HOST_DISPLAY_TEMPLATE,
            source_missing=False,
            locally_hidden=True,
        )
        db.add(pc)
        existing_configs[c_id] = pc
        configs_created = 1
        await _retire_legacy_catalog_proxy_host(db, legacy_tag)
    else:
        pc = existing_configs[c_id]
        pc.source_name = c_name
        pc.panel_group_id = local_group_id
        pc.source_missing = False
        pc.source_payload = c_data
        pc.locally_hidden = True
        if protocol is not None:
            pc.protocol = protocol
        if network is not None:
            pc.network = network
        if port is not None:
            pc.port = port
        if not pc.display_name_template:
            pc.display_name_template = DEFAULT_OC_HOST_DISPLAY_TEMPLATE
        await _retire_legacy_catalog_proxy_host(db, pc.virtual_inbound_tag or legacy_tag)

    return existing_configs[c_id], configs_created, hosts_created
