"""Denied global side-effect bundles must not partially execute."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, load_admin_attrs
from app.db.crud.core import get_core_config_by_id
from app.db.crud.user import create_user
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User
from app.models.core import CoreCreate
from app.models.user import UserCreate
from app.operation import OperatorType
from app.operation.core import CoreOperation
from app.operation.node import NodeOperation
from app.operation.user import UserOperation
from test_integration_admin import owner_admin_details


async def _tenant(db) -> int:
    t = Tenant(name=f"se_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"se_adm_{uuid.uuid4().hex[:6]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    await load_admin_attrs(row, load_role=True, load_users=False, load_usage_logs=False)
    return row


@pytest.mark.asyncio
async def test_non_owner_global_users_reset_leaves_usage_unchanged():
    user_op = UserOperation(OperatorType.API)
    node_op = NodeOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_row = await _administrator(db, tenant_id)
        user = await create_user(
            db,
            UserCreate(username=f"se_u_{uuid.uuid4().hex[:8]}", proxy_settings={}),
            groups=[],
            admin=admin_row,
        )
        user.used_traffic = 42_000
        await db.commit()
        user_id = user.id

        details = build_admin_details(admin_row, include_loaded_metrics=False)
        with patch.object(node_op, "_restart_all_impl", AsyncMock()):
            with pytest.raises(HTTPException) as exc:
                await user_op.reset_all_users_data_usage_and_reconnect_nodes(db, details, node_op)
            assert exc.value.status_code == 403

        row = await db.get(User, user_id)
        assert row is not None
        assert row.used_traffic == 42_000
        await db.rollback()


@pytest.mark.asyncio
async def test_owner_global_users_reset_applies_usage_then_restart():
    user_op = UserOperation(OperatorType.API)
    node_op = NodeOperation(OperatorType.API)
    owner = owner_admin_details()
    async with GetDB() as db:
        admin_role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin_row = Admin(
            username=f"se_owner_u_{uuid.uuid4().hex[:6]}",
            hashed_password="x",
            role_id=admin_role.id,
            tenant_id=None,
        )
        db.add(admin_row)
        await db.flush()
        user = await create_user(
            db,
            UserCreate(username=f"se_ou_{uuid.uuid4().hex[:8]}", proxy_settings={}),
            groups=[],
            admin=admin_row,
        )
        user.used_traffic = 9_000
        await db.commit()
        user_id = user.id

        restart_mock = AsyncMock()
        with patch.object(node_op, "_restart_all_impl", restart_mock):
            await user_op.reset_all_users_data_usage_and_reconnect_nodes(db, owner, node_op)

        row = await db.get(User, user_id)
        assert row is not None
        assert row.used_traffic == 0
        restart_mock.assert_awaited_once()
        await db.rollback()


@pytest.mark.asyncio
async def test_tenant_admin_core_modify_with_restart_nodes_does_not_mutate_core():
    core_op = CoreOperation(OperatorType.API)
    node_op = NodeOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_row = await _administrator(db, tenant_id)
        details = build_admin_details(admin_row, include_loaded_metrics=False)

        db_core = await get_core_config_by_id(db, 1)
        assert db_core is not None
        original_name = db_core.name
        modified = CoreCreate(
            name=f"{original_name}_sidefx",
            config=db_core.config,
            type=db_core.type,
            exclude_inbound_tags=set(db_core.exclude_inbound_tags or []),
            fallbacks_inbound_tags=set(db_core.fallbacks_inbound_tags or []),
        )

        with patch.object(node_op, "_restart_all_impl", AsyncMock()):
            with pytest.raises(HTTPException) as exc:
                await node_op.require_owner_for_global_node_restart(details)
                await core_op.modify_core(db, 1, modified, details)
            assert exc.value.status_code == 403

        db_core_after = await get_core_config_by_id(db, 1)
        assert db_core_after is not None
        assert db_core_after.name == original_name
        await db.rollback()


@pytest.mark.asyncio
async def test_tenant_admin_core_modify_without_restart_nodes_still_allowed():
    core_op = CoreOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_row = await _administrator(db, tenant_id)
        details = build_admin_details(admin_row, include_loaded_metrics=False)

        db_core = await get_core_config_by_id(db, 1)
        assert db_core is not None
        new_name = f"core_ok_{uuid.uuid4().hex[:6]}"
        modified = CoreCreate(
            name=new_name,
            config=db_core.config,
            type=db_core.type,
            exclude_inbound_tags=set(db_core.exclude_inbound_tags or []),
            fallbacks_inbound_tags=set(db_core.fallbacks_inbound_tags or []),
        )

        with patch.object(core_op, "_refresh_hosts_from_db", AsyncMock()):
            with patch("app.operation.core.core_manager.update_core", AsyncMock()):
                response = await core_op.modify_core(db, 1, modified, details)
        assert response.name == new_name

        db_core_after = await get_core_config_by_id(db, 1)
        assert db_core_after is not None
        assert db_core_after.name == new_name
        await db.rollback()
