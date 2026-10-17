"""Phase 3-G: workspace-scoped HWID management API and feature flag."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.hwid import register_user_hwid
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User, UserStatus
from app.operation import OperatorType
from app.operation.hwid import HWIDOperation
from app.operation.hwid_management import HWIDManagementOperation
from app.operation.subscription import SubscriptionOperation
from config import hwid_management_settings


async def _tenant(db) -> int:
    t = Tenant(name=f"3g_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"3g_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


async def _user(db, admin: Admin, name: str) -> User:
    user = User(username=name, admin_id=admin.id, workspace_id=admin.workspace_id, status=UserStatus.active)
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_disabled_flag_returns_503(monkeypatch):
    monkeypatch.setattr(hwid_management_settings, "enabled", False)
    op = HWIDManagementOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        with pytest.raises(Exception) as exc:
            await op.get_summary(db, details)
        assert getattr(exc.value, "status_code", None) == 503


@pytest.mark.asyncio
async def test_workspace_isolation_for_hwid_management(monkeypatch):
    monkeypatch.setattr(hwid_management_settings, "enabled", True)
    mgmt = HWIDManagementOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        await register_user_hwid(db, user_b.id, f"hwid_{uuid.uuid4().hex[:8]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        with pytest.raises(Exception) as exc:
            await mgmt.get_user_detail(db, user_b.id, details_a)
        assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_cross_workspace_revoke_denied(monkeypatch):
    monkeypatch.setattr(hwid_management_settings, "enabled", True)
    mgmt = HWIDManagementOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        await register_user_hwid(db, user_b.id, f"hwid_{uuid.uuid4().hex[:8]}")
        from app.db.crud.hwid import get_user_hwids

        device_id = (await get_user_hwids(db, user_b.id))[0].id
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        with pytest.raises(Exception) as exc:
            await mgmt.revoke_device(db, device_id, details_a)
        assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_owner_can_view_cross_workspace_user(monkeypatch):
    monkeypatch.setattr(hwid_management_settings, "enabled", True)
    mgmt = HWIDManagementOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_b = await _administrator(db, tenant_id)
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        await register_user_hwid(db, user_b.id, f"hwid_{uuid.uuid4().hex[:8]}")
        owner_role = (await db.execute(select(AdminRole).where(AdminRole.is_owner.is_(True)))).scalar_one()
        owner = Admin(
            username=f"own_{uuid.uuid4().hex[:8]}",
            hashed_password="x",
            role_id=owner_role.id,
            tenant_id=tenant_id,
        )
        db.add(owner)
        await db.flush()
        details_owner = build_admin_details(owner, include_loaded_metrics=False)
        detail = await mgmt.get_user_detail(db, user_b.id, details_owner)
        assert detail.user.username == user_b.username
        assert detail.user.device_count == 1


@pytest.mark.asyncio
async def test_reset_requires_delete_permission_scope(monkeypatch):
    monkeypatch.setattr(hwid_management_settings, "enabled", True)
    mgmt = HWIDManagementOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = await _user(db, admin, f"u_{uuid.uuid4().hex[:6]}")
        await register_user_hwid(db, user.id, f"hwid_{uuid.uuid4().hex[:8]}")
        details = build_admin_details(admin, include_loaded_metrics=False)
        result = await mgmt.reset_user_devices(db, user.id, details)
        assert result.count == 1
        detail = await mgmt.get_user_detail(db, user.id, details)
        assert detail.user.device_count == 0


@pytest.mark.asyncio
async def test_legacy_hwid_api_unchanged_when_management_flag_off(monkeypatch):
    """Existing /api/user/{id}/hwids path must work regardless of HWID_MANAGEMENT_ENABLED."""
    monkeypatch.setattr(hwid_management_settings, "enabled", False)
    legacy = HWIDOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await legacy.get_user_hwids(db, user_b.id, details_a)
            raise AssertionError("cross-workspace must fail")
        except HTTPException as exc:
            assert exc.status_code == 404


@pytest.mark.asyncio
async def test_subscription_hwid_validation_unchanged_by_management_flag(monkeypatch):
    monkeypatch.setattr(hwid_management_settings, "enabled", False)
    sub_op = SubscriptionOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = await _user(db, admin, f"u_{uuid.uuid4().hex[:6]}")
        user.hwid_limit = 1
        await db.commit()
        # HWID disabled globally in test DB — validation should no-op without error
        await sub_op.validate_and_register_hwid(
            db,
            user.id,
            user.hwid_limit,
            None,
            x_hwid="test-hwid-value",
            x_device_os=None,
            x_ver_os=None,
            x_device_model=None,
        )
