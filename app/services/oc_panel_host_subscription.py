"""Materialize OC panel hosts from discovery-user subscription content."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelGroup
from app.services.oc_panel_materialization import oc_config_import_eligible
from app.services.oc_connection_credentials import call_oc_panel_api
from app.services.oc_integration_client import OcIntegrationApiError
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import decode_subscription_links, is_importable_configuration_link
from app.services.oc_upstream_subscription import fetch_upstream_subscription_body
from app.utils.logger import get_logger

logger = get_logger("oc-panel-host-sub")


async def _selected_group_ids(db: AsyncSession, panel_id: int) -> list[str]:
    from sqlalchemy import select

    rows = (
        await db.execute(
            select(OCPanelGroup.source_group_id).where(
                OCPanelGroup.panel_id == panel_id,
                OCPanelGroup.is_selected.is_(True),
            )
        )
    ).scalars().all()
    return list(rows)


async def refresh_panel_hosts_from_discovery_subscription(
    db: AsyncSession,
    panel: OCPanel,
) -> int:
    """
    Assign the discovery user to selected OC groups, fetch its subscription, and
    reconcile ``OCPanelDestinationHost`` rows (never catalog ``OCPanelConfig`` hosts).
    """
    if not panel.test_user_id:
        logger.info("oc_host_sub_skip panel=%s reason=no_test_user", panel.id)
        return 0

    groups = await _selected_group_ids(db, panel.id)
    if not groups:
        logger.info("oc_host_sub_skip panel=%s reason=no_selected_groups", panel.id)
        return 0

    catalog_rows = (
        await db.execute(
            select(OCPanelConfig).where(
                OCPanelConfig.panel_id == panel.id,
                OCPanelConfig.source_missing.is_(False),
            )
        )
    ).scalars().all()
    catalog_config_ids: list[str] = []
    for row in catalog_rows:
        payload = row.source_payload if isinstance(row.source_payload, dict) else {}
        mapping = payload.get("group_mapping") or {}
        if oc_config_import_eligible(mapping, groups):
            catalog_config_ids.append(row.source_config_id)

    base = f"/v1/integration/panels/{panel.source_panel_id}"
    path = f"{base}/users/{panel.test_user_id}"
    body: dict[str, Any] = {"groups": groups, "configs": catalog_config_ids}

    try:
        user_data = await call_oc_panel_api(db, panel, "PUT", path, body)
    except OcIntegrationApiError as exc:
        if exc.code in ("CONFIG_NOT_ALLOWED", "CONFIG_NOT_IN_GROUPS") and catalog_config_ids:
            logger.warning(
                "oc_host_sub_put_retry_groups_only panel=%s code=%s",
                panel.id,
                exc.code,
            )
            body = {"groups": groups, "configs": []}
            user_data = await call_oc_panel_api(db, panel, "PUT", path, body)
        elif exc.code in ("CONFIG_NOT_ALLOWED", "CONFIG_NOT_IN_GROUPS"):
            logger.warning(
                "oc_host_sub_put_failed panel=%s code=%s",
                panel.id,
                exc.code,
            )
            return 0
        else:
            raise
    subscription_url = str((user_data or {}).get("subscription_url") or "").strip()
    if not subscription_url:
        logger.warning("oc_host_sub_no_url panel=%s test_user=%s", panel.id, panel.test_user_id)
        return 0

    try:
        sub_body = await fetch_upstream_subscription_body(subscription_url, "links")
    except Exception as exc:
        logger.warning(
            "oc_host_sub_fetch_failed panel=%s error=%s",
            panel.id,
            type(exc).__name__,
        )
        return 0

    raw_links = decode_subscription_links(sub_body if isinstance(sub_body, str) else sub_body.decode())
    links = [link for link in raw_links if is_importable_configuration_link(link)]
    updated = await reconcile_panel_destination_hosts(db, panel, links)
    from app.services.oc_panel_destination_hosts import ensure_panel_destination_hosts_materialized

    await ensure_panel_destination_hosts_materialized(db, panel)
    await db.flush()
    return updated
