"""OC panel_type backfill must not overwrite existing values or invent types."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.models import Tenant, TenantStatus
from app.db.models_oc import OCIntegration, OCPanel, TenantTelegramConnection, TenantTelegramConnectionStatus
from app.services.oc_panel_type_backfill import backfill_null_panel_types


@pytest.mark.asyncio
async def test_backfill_fills_null_only_from_account_panels():
    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        tenant = Tenant(name="backfill_test_tenant", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        conn = TenantTelegramConnection(
            tenant_id=tenant.id,
            oc_account_id=77,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_connection_token_encrypted="enc",
        )
        db.add(conn)
        await db.flush()
        existing = OCPanel(
            integration_id=intg.id,
            tenant_id=tenant.id,
            oc_account_id=77,
            source_panel_id="100",
            purchaser_identity="b",
            name="HasType",
            panel_type="PASARGUARD",
        )
        missing = OCPanel(
            integration_id=intg.id,
            tenant_id=tenant.id,
            oc_account_id=77,
            source_panel_id="101",
            purchaser_identity="b",
            name="NoType",
            panel_type=None,
        )
        db.add_all([existing, missing])
        await db.commit()

        oc_payload = {
            "items": [
                {"subscription_id": 100, "panel_type": "PASARGUARD", "name": "A", "status": "ACTIVE"},
                {"subscription_id": 101, "panel_type": "REBECCA", "name": "B", "status": "ACTIVE"},
            ]
        }

        with patch(
            "app.services.oc_panel_type_backfill.get_active_integration",
            new=AsyncMock(return_value=intg),
        ), patch(
            "app.services.oc_connection_credentials.connection_token_by_id",
            new=AsyncMock(return_value="tok"),
        ), patch(
            "app.services.oc_panel_type_backfill.call_oc_api",
            new=AsyncMock(return_value=oc_payload),
        ):
            report = await backfill_null_panel_types(db, tenant_id=tenant.id, dry_run=False)

        assert report.panels_updated == 1
        refreshed = (await db.execute(select(OCPanel).where(OCPanel.id == missing.id))).scalar_one()
        assert refreshed.panel_type == "REBECCA"
        kept = (await db.execute(select(OCPanel).where(OCPanel.id == existing.id))).scalar_one()
        assert kept.panel_type == "PASARGUARD"
        await db.rollback()
