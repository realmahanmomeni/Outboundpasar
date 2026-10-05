"""Phase 10: Owner global monitoring, visibility, and isolation boundaries."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, load_admin_attrs
from app.db.crud.user import create_user
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.db.models_oc import OCIntegration, OCPanel
from app.models.admin import AdminDetails, AdminListQuery, AdminRoleData
from app.models.user import UserCreate
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.operation.admin_role import AdminRoleOperation
from app.operation.system import SystemOperation
from app.operation.user import UserOperation
from app.routers.authentication import require_owner
from app.routers.panel import list_panels
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID
from app.services.workspace_scope import resolve_admin_workspace_id, resolve_tenant_workspace_scope
from test_workspace_isolation_util import unique_username


async def _tenant(db, label: str = "p10") -> int:
    t = Tenant(name=f"{label}_{uuid.uuid4().hex[:10]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=unique_username("adm"), hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


def _owner_details() -> AdminDetails:
    return AdminDetails(
        id=1,
        username=unique_username("owner"),
        tenant_id=None,
        role=AdminRoleData(is_owner=True, name="owner", id=1),
    )


@pytest.mark.asyncio
async def test_owner_resolve_admin_workspace_id_is_none():
    owner = _owner_details()
    async with GetDB() as db:
        assert await resolve_admin_workspace_id(db, owner, True) is None
        assert await resolve_admin_workspace_id(db, owner, False) is None
        await db.rollback()


@pytest.mark.asyncio
async def test_owner_tenant_workspace_scope_is_global():
    owner = _owner_details()
    async with GetDB() as db:
        tenant_id, workspace_id = await resolve_tenant_workspace_scope(db, owner)
        assert tenant_id is None
        assert workspace_id is None
        await db.rollback()


@pytest.mark.asyncio
async def test_owner_system_user_stats_span_workspaces():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await create_user(
            db, UserCreate(username=unique_username("ua"), proxy_settings={}), groups=[], admin=admin_a
        )
        await create_user(
            db, UserCreate(username=unique_username("ub"), proxy_settings={}), groups=[], admin=admin_a
        )
        await create_user(
            db, UserCreate(username=unique_username("uc"), proxy_settings={}), groups=[], admin=admin_b
        )
        owner = _owner_details()
        stats = await SystemOperation.get_system_users_stats(db, owner)
        assert stats.total_user >= 3
        await db.rollback()


@pytest.mark.asyncio
async def test_administrator_system_stats_remain_workspace_scoped():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await create_user(
            db, UserCreate(username=unique_username("ua"), proxy_settings={}), groups=[], admin=admin_a
        )
        await create_user(
            db, UserCreate(username=unique_username("ub"), proxy_settings={}), groups=[], admin=admin_b
        )
        await create_user(
            db, UserCreate(username=unique_username("ub2"), proxy_settings={}), groups=[], admin=admin_b
        )
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        stats = await SystemOperation.get_system_users_stats(db, details_a)
        assert stats.total_user == 1
        await db.rollback()


@pytest.mark.asyncio
async def test_owner_drill_down_user_in_foreign_workspace():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        foreign_user = await create_user(
            db,
            UserCreate(username=unique_username("foreign"), proxy_settings={}),
            groups=[],
            admin=admin_b,
        )
        owner = _owner_details()
        resolved = await user_op.get_validated_user_by_id(db, foreign_user.id, owner)
        assert resolved.id == foreign_user.id
        await db.rollback()


@pytest.mark.asyncio
async def test_owner_lists_panels_across_workspaces():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = OCIntegration(
            base_url="https://oc.example",
            api_token_encrypted="enc",
            token_preview="prev",
            is_active=True,
        )
        db.add(integ)
        await db.flush()
        panel_a = OCPanel(
            integration_id=integ.id,
            source_panel_id=str(uuid.uuid4().int)[:8],
            purchaser_identity="1",
            name="A",
            tenant_id=tenant_id,
            workspace_id=admin_a.workspace_id,
            oc_account_id=1,
        )
        panel_b = OCPanel(
            integration_id=integ.id,
            source_panel_id=str(uuid.uuid4().int)[:8],
            purchaser_identity="2",
            name="B",
            tenant_id=tenant_id,
            workspace_id=admin_b.workspace_id,
            oc_account_id=2,
        )
        db.add_all([panel_a, panel_b])
        await db.flush()
        owner = _owner_details()
        listed = await list_panels(db, (owner.username, True, owner))
        ids = {p.id for p in listed}
        assert panel_a.id in ids and panel_b.id in ids
        await db.rollback()


@pytest.mark.asyncio
async def test_owner_admin_list_not_workspace_filtered():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        owner = _owner_details()
        resp = await admin_op.get_admins(db, AdminListQuery(), owner)
        names = {a.username for a in resp.admins}
        assert admin_a.username in names
        assert admin_b.username in names
        await db.rollback()


@pytest.mark.asyncio
async def test_owner_cross_tenant_admin_lookup_allowed():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_a = await _tenant(db, "ta")
        tenant_b = await _tenant(db, "tb")
        admin_b = await _administrator(db, tenant_b)
        owner = _owner_details()
        fetched = await admin_op.get_validated_admin_by_id(db, admin_b.id, current_admin=owner)
        assert fetched.id == admin_b.id
        await db.rollback()


@pytest.mark.asyncio
async def test_owner_system_stats_admin_username_cross_workspace():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await create_user(
            db, UserCreate(username=unique_username("ub"), proxy_settings={}), groups=[], admin=admin_b
        )
        await load_admin_attrs(admin_b, load_users=False, load_usage_logs=False, load_role=True)
        owner = _owner_details()
        stats = await SystemOperation.get_system_users_stats(db, owner, admin_username=admin_b.username)
        assert stats.total_user == 1
        await db.rollback()


@pytest.mark.asyncio
async def test_non_owner_blocked_by_require_owner_dependency():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await require_owner(details_a)
            raise AssertionError("tenant administrator must not pass require_owner")
        except HTTPException as exc:
            assert exc.status_code == 403
        await db.rollback()


@pytest.mark.asyncio
async def test_operator_cannot_use_owner_global_admin_list_scope():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_role = (await db.execute(select(AdminRole).where(AdminRole.id == BUILTIN_OPERATOR_ROLE_ID))).scalar_one()
        op_row = Admin(
            username=unique_username("op"),
            hashed_password="x",
            role_id=op_role.id,
            tenant_id=tenant_id,
        )
        db.add(op_row)
        await db.flush()
        creator_details = build_admin_details(admin_a, include_loaded_metrics=False)
        await assign_workspace_for_new_admin(db, op_row, creator=creator_details)
        await db.refresh(op_row)
        await load_admin_attrs(op_row, load_role=True, load_users=False, load_usage_logs=False)
        op_details = build_admin_details(op_row, include_loaded_metrics=False)
        resp = await admin_op.get_admins(db, AdminListQuery(), op_details)
        names = {a.username for a in resp.admins}
        assert admin_b.username not in names
        assert op_row.username in names or admin_a.username in names
        await db.rollback()


@pytest.mark.asyncio
async def test_owner_passes_require_owner():
    owner = _owner_details()
    result = await require_owner(owner)
    assert result.is_owner
