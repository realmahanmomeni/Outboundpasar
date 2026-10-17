"""Repair empty local OC catalog when OC is the runtime source of truth."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelGroup
from app.services.oc_connection_credentials import panel_has_active_connection

logger = logging.getLogger(__name__)


async def panel_local_catalog_empty(db: AsyncSession, panel_id: int) -> bool:
    groups = int(
        await db.scalar(
            select(func.count()).select_from(OCPanelGroup).where(OCPanelGroup.panel_id == panel_id)
        )
        or 0
    )
    configs = int(
        await db.scalar(
            select(func.count())
            .select_from(OCPanelConfig)
            .where(OCPanelConfig.panel_id == panel_id, OCPanelConfig.source_missing.is_(False))
        )
        or 0
    )
    return groups == 0 and configs == 0


async def ensure_panel_catalog_from_oc(db: AsyncSession, panel: OCPanel) -> bool:
    """
    Idempotently mirror groups/configs from OC when the local catalog is empty.

    Returns True when ``sync_panel_from_outbound_center`` ran successfully.
    """
    if panel.tenant_id is None:
        return False
    if not await panel_has_active_connection(db, panel):
        return False
    if not await panel_local_catalog_empty(db, panel.id):
        return False

    from app.node.oc_sync import sync_panel_from_outbound_center

    logger.info("oc_catalog_repair panel_id=%s source_panel_id=%s", panel.id, panel.source_panel_id)
    await sync_panel_from_outbound_center(db, panel.id)
    return True
