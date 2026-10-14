"""Panel statistics aggregation from oc_user_mappings."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.panel_statistics import aggregate_panel_statistics
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User, UserStatus
from app.db.models_oc import OCIntegration, OCUserMapping, OCPanel
from app.operation import OperatorType
from app.operation.panel_statistics import PanelStatisticsOperation
from app.models.panel_statistics import PanelStatisticsListResponse
from config import panel_statistics_settings


async def _tenant(db) -> int:
    t = Tenant(name=f"ps_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"ps_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


async def _integration(db) -> OCIntegration:
    row = OCIntegration(base_url="https://example.com", api_token_encrypted="x", token_preview="prev")
    db.add(row)
    await db.flush()
    return row


async def _connected_oc_filter(_self, db, admin, *, is_owner: bool):
    return admin.tenant_id, "connected", None


@pytest.mark.asyncio
async def test_mapping_aggregate_counts_and_traffic():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        integration = await _integration(db)
        panel = OCPanel(
            integration_id=integration.id,
            source_panel_id="src-1",
            purchaser_identity="buyer",
            name="Panel One",
            tenant_id=tenant_id,
            workspace_id=admin.workspace_id,
            oc_account_id=1,
        )
        db.add(panel)
        await db.flush()
        user1 = User(
            username=f"u1_{uuid.uuid4().hex[:6]}",
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            status=UserStatus.active,
        )
        user2 = User(
            username=f"u2_{uuid.uuid4().hex[:6]}",
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            status=UserStatus.expired,
        )
        db.add_all([user1, user2])
        await db.flush()
        db.add_all(
            [
                OCUserMapping(
                    user_id=user1.id,
                    panel_id=panel.id,
                    external_user_id="ext-1",
                    last_cumulative_traffic=1000,
                    last_synced_at=datetime.now(UTC),
                ),
                OCUserMapping(
                    user_id=user2.id,
                    panel_id=panel.id,
                    external_user_id="ext-2",
                    last_cumulative_traffic=2500,
                    last_synced_at=datetime.now(UTC),
                ),
            ]
        )
        await db.commit()

        rows = await aggregate_panel_statistics(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant_id,
            oc_account_id=None,
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["panel_id"] == panel.id
        assert row["total_users"] == 2
        assert row["active_users"] == 1
        assert row["traffic_used"] == 3500
        assert row["expiration_distribution"]["active"] == 1
        assert row["expiration_distribution"]["expired"] == 1


@pytest.mark.asyncio
async def test_same_user_on_multiple_panels_counted_per_panel():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        integration = await _integration(db)
        panel_a = OCPanel(
            integration_id=integration.id,
            source_panel_id="a",
            purchaser_identity="buyer",
            name="A",
            tenant_id=tenant_id,
            workspace_id=admin.workspace_id,
            oc_account_id=1,
        )
        panel_b = OCPanel(
            integration_id=integration.id,
            source_panel_id="b",
            purchaser_identity="buyer",
            name="B",
            tenant_id=tenant_id,
            workspace_id=admin.workspace_id,
            oc_account_id=1,
        )
        db.add_all([panel_a, panel_b])
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            status=UserStatus.active,
        )
        db.add(user)
        await db.flush()
        db.add_all(
            [
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel_a.id,
                    external_user_id="ext-a",
                    last_cumulative_traffic=500,
                ),
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel_b.id,
                    external_user_id="ext-b",
                    last_cumulative_traffic=700,
                ),
            ]
        )
        await db.commit()

        rows = await aggregate_panel_statistics(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant_id,
            oc_account_id=None,
        )
        by_name = {r["panel_name"]: r for r in rows}
        assert by_name["A"]["total_users"] == 1
        assert by_name["A"]["traffic_used"] == 500
        assert by_name["B"]["total_users"] == 1
        assert by_name["B"]["traffic_used"] == 700


@pytest.mark.asyncio
async def test_workspace_isolation_via_panel_scope(monkeypatch):
    monkeypatch.setattr(panel_statistics_settings, "enabled", True)
    monkeypatch.setattr(PanelStatisticsOperation, "_tenant_oc_panel_filter", _connected_oc_filter)
    op = PanelStatisticsOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integration = await _integration(db)
        panel_a = OCPanel(
            integration_id=integration.id,
            source_panel_id="wa",
            purchaser_identity="buyer",
            name="Panel A",
            tenant_id=tenant_id,
            workspace_id=admin_a.workspace_id,
            oc_account_id=1,
        )
        panel_b = OCPanel(
            integration_id=integration.id,
            source_panel_id="wb",
            purchaser_identity="buyer",
            name="Panel B",
            tenant_id=tenant_id,
            workspace_id=admin_b.workspace_id,
            oc_account_id=2,
        )
        db.add_all([panel_a, panel_b])
        await db.flush()
        user_a = User(
            username=f"ua_{uuid.uuid4().hex[:6]}",
            admin_id=admin_a.id,
            workspace_id=admin_a.workspace_id,
            status=UserStatus.active,
        )
        db.add(user_a)
        await db.flush()
        db.add(
            OCUserMapping(
                user_id=user_a.id,
                panel_id=panel_a.id,
                external_user_id="ext-a",
                last_cumulative_traffic=9000,
            )
        )
        await db.commit()

        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)
        list_a = await op.list_panels(db, details_a)
        list_b = await op.list_panels(db, details_b)
        assert isinstance(list_a, PanelStatisticsListResponse)
        names_a = {p.panel_name: p for p in list_a.panels}
        names_b = {p.panel_name: p for p in list_b.panels}
        assert names_a["Panel A"].traffic_used == 9000
        assert "Panel B" not in names_a
        assert "Panel B" in names_b
        assert names_b["Panel B"].traffic_used == 0
        assert "Panel A" not in names_b


@pytest.mark.asyncio
async def test_empty_panel_returns_zero_values():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        integration = await _integration(db)
        panel = OCPanel(
            integration_id=integration.id,
            source_panel_id="empty",
            purchaser_identity="buyer",
            name="Empty Panel",
            tenant_id=tenant_id,
            workspace_id=admin.workspace_id,
            oc_account_id=1,
        )
        db.add(panel)
        await db.commit()

        rows = await aggregate_panel_statistics(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant_id,
            oc_account_id=None,
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["total_users"] == 0
        assert row["active_users"] == 0
        assert row["traffic_used"] == 0


@pytest.mark.asyncio
async def test_list_panels_response_shape_unchanged(monkeypatch):
    monkeypatch.setattr(panel_statistics_settings, "enabled", True)
    monkeypatch.setattr(PanelStatisticsOperation, "_tenant_oc_panel_filter", _connected_oc_filter)
    op = PanelStatisticsOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        integration = await _integration(db)
        panel = OCPanel(
            integration_id=integration.id,
            source_panel_id="shape",
            purchaser_identity="buyer",
            name="Shape Panel",
            tenant_id=tenant_id,
            workspace_id=admin.workspace_id,
            oc_account_id=1,
        )
        db.add(panel)
        await db.commit()
        details = build_admin_details(admin, include_loaded_metrics=False)
        response = await op.list_panels(db, details)
        assert hasattr(response, "panels")
        if response.panels:
            item = response.panels[0]
            assert hasattr(item, "panel_id")
            assert hasattr(item, "panel_name")
            assert hasattr(item, "sync_status")
            assert hasattr(item, "total_users")
            assert hasattr(item, "active_users")
            assert hasattr(item, "traffic_used")
            assert hasattr(item, "last_activity_at")
            assert hasattr(item, "expiration_distribution")
