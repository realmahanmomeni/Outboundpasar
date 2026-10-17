"""Regression: empty local catalog is repaired from OC on panel read."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.db import GetDB
from app.db.models import Tenant, TenantStatus
from app.db.models_oc import OCPanel, OCPanelGroup, OCIntegration
from app.services.oc_panel_catalog_repair import ensure_panel_catalog_from_oc, panel_local_catalog_empty


@pytest.mark.asyncio
async def test_ensure_panel_catalog_from_oc_skips_when_catalog_present():
    async with GetDB() as db:
        integ = OCIntegration(
            base_url="https://oc.example",
            api_token_encrypted="e",
            token_preview="t",
        )
        db.add(integ)
        await db.flush()
        tenant = Tenant(name=f"cat_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        panel = OCPanel(
            integration_id=integ.id,
            source_panel_id="sub1",
            purchaser_identity="b",
            name="P",
            tenant_id=tenant.id,
        )
        db.add(panel)
        await db.flush()
        db.add(
            OCPanelGroup(
                panel_id=panel.id,
                source_group_id="g1",
                source_name="G",
                is_selected=True,
            )
        )
        await db.commit()

        with patch(
            "app.node.oc_sync.sync_panel_from_outbound_center",
            new=AsyncMock(),
        ) as sync_mock:
            ran = await ensure_panel_catalog_from_oc(db, panel)
        assert ran is False
        sync_mock.assert_not_called()


@pytest.mark.asyncio
async def test_panel_local_catalog_empty_true_when_no_rows():
    async with GetDB() as db:
        integ = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(integ)
        await db.flush()
        panel = OCPanel(
            integration_id=integ.id,
            source_panel_id=f"sp_{uuid.uuid4().hex[:6]}",
            purchaser_identity="b",
            name="Empty",
        )
        db.add(panel)
        await db.commit()
        assert await panel_local_catalog_empty(db, panel.id) is True
