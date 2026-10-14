"""Phase 3-A: tenant isolation audit regression tests (authoritative scope resolution)."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.user import create_user
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Group, Tenant, TenantStatus, User
from app.db.models_oc import OCIntegration, OCPanel
from app.models.group import GroupCreate
from app.models.user import BulkUsersSelection, UserCreate, UserListQuery
from app.models.user_panel_binding import UserPanelBindingCreate
from app.operation import OperatorType
from app.operation.group import GroupOperation
from app.operation.user import UserOperation
from app.operation.user_panel_binding import UserPanelBindingOperation


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def _tenant(db) -> Tenant:
    row = Tenant(name=_uid("p3a"), status=TenantStatus.active)
    db.add(row)
    await db.flush()
    return row


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.id == 2))).scalar_one()
    row = Admin(username=_uid("adm"), hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_user_list_resolves_tenant_from_db_when_claims_missing():
    """List/count queries must not skip tenant filter when JWT claims omit tenant_id."""
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_a = await _tenant(db)
        tenant_b = await _tenant(db)
        admin_a = await _administrator(db, tenant_a.id)
        admin_b = await _administrator(db, tenant_b.id)
        marker_b = _uid("user_b")
        await create_user(
            db,
            UserCreate(username=marker_b, proxy_settings={}),
            groups=[],
            admin=admin_b,
        )
        await db.commit()

        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        details_a.tenant_id = None
        details_a.workspace_id = None

        resp = await user_op.get_users(db, details_a, UserListQuery())
        usernames = {u.username for u in resp.users}
        assert marker_b not in usernames


@pytest.mark.asyncio
async def test_bulk_user_delete_cross_tenant_fails_closed():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_a = await _tenant(db)
        tenant_b = await _tenant(db)
        admin_a = await _administrator(db, tenant_a.id)
        admin_b = await _administrator(db, tenant_b.id)
        user_b = await create_user(
            db,
            UserCreate(username=_uid("ub"), proxy_settings={}),
            groups=[],
            admin=admin_b,
        )
        await db.commit()
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        with pytest.raises(HTTPException) as exc:
            await user_op.bulk_remove_users(db, BulkUsersSelection(ids={user_b.id}), details_a)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cross_tenant_panel_binding_rejected():
    bind_op = UserPanelBindingOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_a = await _tenant(db)
        tenant_b = await _tenant(db)
        admin_a = await _administrator(db, tenant_a.id)
        admin_b = await _administrator(db, tenant_b.id)
        user_a = await create_user(
            db,
            UserCreate(username=_uid("ua"), proxy_settings={}),
            groups=[],
            admin=admin_a,
        )
        intg = OCIntegration(
            base_url="http://phase3a-test",
            api_token_encrypted="enc",
            token_preview="p3a",
            is_active=False,
        )
        db.add(intg)
        await db.flush()
        panel_b = OCPanel(
            integration_id=intg.id,
            source_panel_id=_uid("src"),
            purchaser_identity=admin_b.username,
            tenant_id=tenant_b.id,
            workspace_id=admin_b.workspace_id,
            name="Panel B",
        )
        db.add(panel_b)
        await db.commit()

        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        with pytest.raises(HTTPException) as exc:
            await bind_op.create_binding(
                db,
                user_a.id,
                UserPanelBindingCreate(oc_panel_id=panel_b.id, enabled=True),
                details_a,
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_group_delete_user_enumeration_uses_resolved_workspace_scope():
    group_op = GroupOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = (await _tenant(db)).id
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        details_a.workspace_id = None
        details_a.tenant_id = None

        inbound_tag = "VLESS TCP REALITY"
        with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
            group_b = await group_op.create_group(
                db,
                GroupCreate(name=_uid("grp")[:16], inbound_tags=[inbound_tag]),
                build_admin_details(admin_b, include_loaded_metrics=False),
            )
        user_b = await create_user(
            db,
            UserCreate(username=_uid("ub"), group_ids=[group_b.id], proxy_settings={}),
            groups=[await db.get(Group, group_b.id)],
            admin=admin_b,
        )
        await db.commit()

        with pytest.raises(HTTPException) as exc:
            await group_op.remove_group(db, group_b.id, details_a)
        assert exc.value.status_code == 404
        assert await db.get(User, user_b.id) is not None
