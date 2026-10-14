"""Phase 3-F: workspace-scoped panel statistics API and feature flag."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User, UserStatus
from app.db.models_oc import OCPanel, OCIntegration, UserPanelBinding
from app.operation import OperatorType
from app.operation.panel_statistics import PanelStatisticsOperation
from config import panel_statistics_settings


async def _tenant(db) -> int:
    t = Tenant(name=f"3f_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"3f_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
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
async def test_disabled_flag_returns_503(monkeypatch):
    monkeypatch.setattr(panel_statistics_settings, "enabled", False)
    op = PanelStatisticsOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        with pytest.raises(Exception) as exc:
            await op.get_overview(db, details)
        assert getattr(exc.value, "status_code", None) == 503


@pytest.mark.asyncio
async def test_workspace_isolation_for_panel_stats(monkeypatch):
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
            source_panel_id="src-a",
            purchaser_identity="buyer-a",
            name="Panel Workspace A",
            tenant_id=tenant_id,
            workspace_id=admin_a.workspace_id,
            oc_account_id=1,
        )
        panel_b = OCPanel(
            integration_id=integration.id,
            source_panel_id="src-b",
            purchaser_identity="buyer-b",
            name="Panel Workspace B",
            tenant_id=tenant_id,
            workspace_id=admin_b.workspace_id,
            oc_account_id=2,
        )
        db.add(panel_a)
        db.add(panel_b)
        await db.flush()
        user_a = User(username=f"u_{uuid.uuid4().hex[:6]}", admin_id=admin_a.id, workspace_id=admin_a.workspace_id, status=UserStatus.active)
        db.add(user_a)
        await db.flush()
        db.add(
            UserPanelBinding(
                tenant_id=tenant_id,
                user_id=user_a.id,
                oc_panel_id=panel_a.id,
                enabled=True,
                source="explicit",
            )
        )
        await db.commit()

        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)

        # Force connected OC filter for both workspaces in tests
        list_a = await op.list_panels(db, details_a)
        list_b = await op.list_panels(db, details_b)
        names_a = {p.panel_name for p in list_a.panels}
        names_b = {p.panel_name for p in list_b.panels}
        assert "Panel Workspace A" in names_a
        assert "Panel Workspace B" not in names_a
        assert "Panel Workspace B" in names_b
        assert "Panel Workspace A" not in names_b


@pytest.mark.asyncio
async def test_overview_scoped_to_workspace_users(monkeypatch):
    monkeypatch.setattr(panel_statistics_settings, "enabled", True)
    monkeypatch.setattr(PanelStatisticsOperation, "_tenant_oc_panel_filter", _connected_oc_filter)
    op = PanelStatisticsOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            status=UserStatus.active,
        )
        db.add(user)
        await db.commit()
        details = build_admin_details(admin, include_loaded_metrics=False)
        overview = await op.get_overview(db, details)
        assert overview.total_users >= 1
        assert overview.active_users >= 1


@pytest.mark.asyncio
async def test_existing_system_stats_unchanged_when_flag_off(monkeypatch):
    """Sanity: disabling panel stats does not alter system operation imports."""
    monkeypatch.setattr(panel_statistics_settings, "enabled", False)
    from app.operation.system import SystemOperation

    stats = await SystemOperation.get_system_resource_stats()
    assert stats.version
