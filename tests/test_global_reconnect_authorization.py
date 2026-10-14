"""Global reconnect-all authorization: owner-only at operation layer."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, load_admin_attrs
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.models.admin import AdminDetails, AdminRoleData
from app.operation import OperatorType
from app.operation.node import NodeOperation
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID
from test_integration_admin import owner_admin_details
from test_workspace_isolation_util import unique_username


async def _tenant(db) -> int:
    t = Tenant(name=f"gr_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
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


def _administrator_details(admin: Admin) -> AdminDetails:
    return build_admin_details(admin, include_loaded_metrics=False)


@pytest.mark.asyncio
async def test_tenant_administrator_cannot_restart_all_nodes():
    node_op = NodeOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        tenant_admin = await _administrator(db, tenant_id)
        details = _administrator_details(tenant_admin)
        assert details.role is not None
        assert details.role.name == "administrator"

        with patch.object(node_op, "_restart_all_impl", AsyncMock()):
            with pytest.raises(HTTPException) as exc:
                await node_op.restart_all_node(db, details, core_id=None)
            assert exc.value.status_code == 403

        await db.rollback()


@pytest.mark.asyncio
async def test_operator_cannot_restart_all_nodes():
    node_op = NodeOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        creator = await _administrator(db, tenant_id)
        op = await _operator(db, tenant_id, creator)
        details = _administrator_details(op)

        with patch.object(node_op, "_restart_all_impl", AsyncMock()):
            with pytest.raises(HTTPException) as exc:
                await node_op.restart_all_node(db, details, core_id=None)
            assert exc.value.status_code == 403

        await db.rollback()


@pytest.mark.asyncio
async def test_owner_can_restart_all_nodes():
    node_op = NodeOperation(OperatorType.API)
    owner = owner_admin_details()
    mock_impl = AsyncMock()
    async with GetDB() as db:
        with patch.object(node_op, "_restart_all_impl", mock_impl):
            await node_op.restart_all_node(db, owner, core_id=1)
        mock_impl.assert_awaited_once_with(db, owner, 1)
        await db.rollback()


@pytest.mark.asyncio
async def test_single_node_reconnect_unaffected_for_tenant_administrator():
    """Per-node reconnect is separate from global restart_all_node."""
    node_op = NodeOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        tenant_admin = await _administrator(db, tenant_id)
        details = _administrator_details(tenant_admin)
        with patch.object(node_op, "connect_single_node", AsyncMock()) as connect_mock:
            await node_op.restart_node(db, 999, details)
        connect_mock.assert_awaited_once_with(db, 999, force_start=True)
        await db.rollback()
