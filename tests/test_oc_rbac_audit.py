"""OC RBAC audit: normal-admin API boundaries (item 6) and related scope regressions."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, load_admin_attrs
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.models.admin import AdminCreate, AdminDetails, AdminListQuery, AdminRoleData, AdminSimpleListQuery, AdminUsageQuery
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.operation.permissions import PermissionDenied, enforce_permission
from app.services.tenant_admin_scope import (
    BUILTIN_ADMINISTRATOR_ROLE_ID,
    BUILTIN_OPERATOR_ROLE_ID,
    assert_tenant_admin_may_assign_role,
)
from test_workspace_isolation_util import unique_username


async def _tenant(db) -> int:
    t = Tenant(name=f"rbac_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
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
    await load_admin_attrs(row, load_role=True, load_users=False, load_usage_logs=False)
    return row


async def _operator(db, tenant_id: int, creator: Admin) -> Admin:
    op_role = (await db.execute(select(AdminRole).where(AdminRole.id == BUILTIN_OPERATOR_ROLE_ID))).scalar_one()
    row = Admin(username=unique_username("op"), hashed_password="x", role_id=op_role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    creator_details = build_admin_details(creator, include_loaded_metrics=False)
    await assign_workspace_for_new_admin(db, row, creator=creator_details)
    await db.refresh(row)
    await load_admin_attrs(row, load_role=True, load_users=False, load_usage_logs=False)
    return row


def _details(admin: Admin) -> AdminDetails:
    return build_admin_details(admin, include_loaded_metrics=False)


@pytest.mark.asyncio
async def test_operator_cannot_create_admin_via_permission_layer():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        creator = await _administrator(db, tenant_id)
        op = await _operator(db, tenant_id, creator)
        details = _details(op)
        for action in ("create", "read", "read_simple", "update", "delete"):
            with pytest.raises(PermissionDenied):
                enforce_permission(details, "admins", action)
        await db.rollback()


@pytest.mark.asyncio
async def test_operator_get_admins_denied_at_operation_layer():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        creator = await _administrator(db, tenant_id)
        op = await _operator(db, tenant_id, creator)
        with pytest.raises(HTTPException) as exc:
            await admin_op.get_admins(db, AdminListQuery(), _details(op))
        assert exc.value.status_code == 403
        await db.rollback()


@pytest.mark.asyncio
async def test_operator_cannot_fetch_peer_admin_usage():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op = await _operator(db, tenant_id, admin_a)
        op_details = _details(op)

        with pytest.raises(HTTPException) as exc:
            await admin_op.get_admin_usage_by_id(db, admin_b.id, op_details, AdminUsageQuery())
        assert exc.value.status_code == 404

        await db.rollback()


@pytest.mark.asyncio
async def test_tenant_administrator_cannot_create_administrator_account():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        tenant_admin = await _administrator(db, tenant_id)
        actor = _details(tenant_admin)
        payload = AdminCreate(
            username=unique_username("newadm"),
            password="TestPassword12!",
            role_id=BUILTIN_ADMINISTRATOR_ROLE_ID,
        )
        with pytest.raises(HTTPException) as exc:
            await admin_op.create_admin(db, payload, actor)
        assert exc.value.status_code == 403
        await db.rollback()


@pytest.mark.asyncio
async def test_tenant_administrator_may_create_operator_in_own_workspace():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        tenant_admin = await _administrator(db, tenant_id)
        actor = _details(tenant_admin)
        username = unique_username("newop")
        payload = AdminCreate(
            username=username,
            password="TestPassword12!",
            role_id=BUILTIN_OPERATOR_ROLE_ID,
        )
        created = await admin_op.create_admin(db, payload, actor)
        assert created.username == username
        assert created.role is not None and created.role.id == BUILTIN_OPERATOR_ROLE_ID
        await db.rollback()


@pytest.mark.asyncio
async def test_tenant_administrator_cannot_read_foreign_workspace_admin_usage():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        actor = _details(admin_a)
        with pytest.raises(HTTPException) as exc:
            await admin_op.get_admin_usage_by_id(db, admin_b.id, actor, AdminUsageQuery())
        assert exc.value.status_code == 404
        await db.rollback()


@pytest.mark.asyncio
async def test_assert_tenant_admin_may_assign_role_blocks_administrator_role():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        tenant_admin = await _administrator(db, tenant_id)
        actor = AdminDetails(
            id=tenant_admin.id,
            username=tenant_admin.username,
            tenant_id=tenant_id,
            workspace_id=tenant_admin.workspace_id,
            role=AdminRoleData(id=2, name="administrator", is_owner=False),
        )
        with pytest.raises(HTTPException) as exc:
            await assert_tenant_admin_may_assign_role(db, actor, BUILTIN_ADMINISTRATOR_ROLE_ID)
        assert exc.value.status_code == 403
        await db.rollback()


@pytest.mark.asyncio
async def test_tenant_admin_admin_filter_api_lists_self_and_own_operators_only():
    """Statistics admin-filter dropdown uses admins/simple; scope is workspace-bound."""
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a)
        op_b = await _operator(db, tenant_id, admin_b)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)

        simple = await admin_op.get_admins_simple(db, AdminSimpleListQuery(), details_a)
        names = {row.username for row in simple.admins}

        assert admin_a.username in names
        assert op_a.username in names
        assert admin_b.username not in names
        assert op_b.username not in names
        assert simple.total == len(simple.admins)

        listed = await admin_op.get_admins(db, AdminListQuery(), details_a)
        list_names = {a.username for a in listed.admins}
        assert admin_b.username not in list_names
        assert op_b.username not in list_names
        assert admin_a.username in list_names
        assert op_a.username in list_names

        await db.rollback()
