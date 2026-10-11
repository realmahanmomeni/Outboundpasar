"""Sync OCPanel metadata from Outbound Center (panel_type, optional buyer auth)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel
from app.services.oc_integration_client import OcIntegrationApiError, call_oc_api
from app.services.oc_panel_buyer_auth import normalize_panel_type, try_refresh_buyer_auth_from_oc
from app.services.oc_panel_import_metadata import apply_panel_metadata_from_oc

logger = logging.getLogger(__name__)


async def refresh_panel_type_from_oc(
    db: AsyncSession,
    panel: OCPanel,
    *,
    connection_token: str,
) -> str | None:
    """Fetch ``GET /v1/integration/panels/{subscription_id}`` and persist ``panel_type`` when present."""
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select

    subscription_id = str(panel.source_panel_id).strip()
    if not subscription_id:
        return panel.panel_type
    integration = panel.integration
    if integration is None:
        row = (
            await db.execute(
                select(OCPanel).options(selectinload(OCPanel.integration)).where(OCPanel.id == panel.id)
            )
        ).scalar_one_or_none()
        integration = row.integration if row else None
    if integration is None:
        return panel.panel_type
    try:
        data = await call_oc_api(
            integration,
            "GET",
            f"/v1/integration/panels/{subscription_id}",
            connection_token=connection_token,
        )
    except OcIntegrationApiError as exc:
        logger.info(
            "oc_panel_type_refresh_failed panel_id=%s source=%s http=%s",
            panel.id,
            subscription_id,
            exc.oc_status,
        )
        return panel.panel_type
    if not isinstance(data, dict):
        return panel.panel_type
    panel_type = normalize_panel_type(str(data.get("panel_type") or ""))
    if panel_type:
        panel.panel_type = panel_type
        await db.flush()
        logger.info(
            "oc_panel_type_refreshed panel_id=%s source=%s panel_type=%s",
            panel.id,
            subscription_id,
            panel_type,
        )
    return panel.panel_type


async def ensure_panel_metadata_for_ops(
    db: AsyncSession,
    panel: OCPanel,
    *,
    connection_token: str | None,
) -> None:
    """Best-effort panel_type (+ buyer auth when OC exposes it) before panel API routing."""
    if connection_token and not (panel.panel_type or "").strip():
        await refresh_panel_type_from_oc(db, panel, connection_token=connection_token)
    if connection_token and normalize_panel_type(panel.panel_type) == "REBECCA":
        await try_refresh_buyer_auth_from_oc(db, panel, connection_token=connection_token)


async def apply_account_panel_row(
    db: AsyncSession,
    panel: OCPanel,
    row: dict[str, Any],
) -> None:
    """Update local panel from ``AccountPanelItem`` JSON (import / available-panels)."""
    await apply_panel_metadata_from_oc(db, panel, row)
