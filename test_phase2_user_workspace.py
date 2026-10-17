"""Phase 2: user workspace isolation within a tenant."""
from __future__ import annotations

import asyncio
import uuid
from test_workspace_isolation_util import unique_username, unique_tag

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.user import create_user
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User
from app.db.models_oc import OCIntegration, OCPanel, OCUserMapping, UserPanelBinding
from app.models.user import BulkUser, UserCreate, UserListQuery
from app.operation.hwid import HWIDOperation
from app.models.user_panel_binding import UserPanelBindingCreate
from app.operation import OperatorType
from app.operation.user import UserOperation
from app.operation.user_panel_binding import UserPanelBindingOperation
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID


async def _tenant(db) -> int:
    t = Tenant(name=f"p2_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int, name: str | None = None) -> Admin:
    username = name or unique_username("adm")
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=username, hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


async def _operator(db, tenant_id: int, creator: Admin, name: str) -> Admin:
    op_role = (await db.execute(select(AdminRole).where(AdminRole.id == BUILTIN_OPERATOR_ROLE_ID))).scalar_one()
    row = Admin(username=name, hashed_password="x", role_id=op_role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    creator_details = build_admin_details(creator, include_loaded_metrics=False)
    await assign_workspace_for_new_admin(db, row, creator=creator_details)
    await db.refresh(row)
    return row


async def _user(db, admin: Admin, name: str) -> User:
    return await create_user(db, UserCreate(username=name, proxy_settings={}), groups=[], admin=admin)


async def test_admin_cannot_list_other_workspace_users():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:6]}")
        await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        resp = await user_op.get_users(db, details_a, UserListQuery())
        usernames = {u.username for u in resp.users}
        assert all(not u.startswith("ub_") for u in usernames)
        assert resp.total == len(resp.users)
        await db.rollback()


async def test_admin_cannot_get_other_workspace_user_by_id():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await user_op.get_user_by_id(db, user_b.id, details_a)
            raise AssertionError("cross-workspace user detail must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_operator_inherits_workspace_user_access():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
        user_a = await _user(db, op_a, f"ua_{uuid.uuid4().hex[:6]}")
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        op_details = build_admin_details(op_a, include_loaded_metrics=False)
        got = await user_op.get_user_by_id(db, user_a.id, op_details)
        assert got.id == user_a.id
        try:
            await user_op.get_user_by_id(db, user_b.id, op_details)
            raise AssertionError("operator must not access other workspace user")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_set_owner_cannot_cross_workspace():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_a = await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:6]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await user_op.set_owner_by_id(db, user_a.id, admin_b.username, details_a)
            raise AssertionError("cross-workspace set_owner must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_panel_binding_cross_workspace_denied():
    bind_op = UserPanelBindingOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_a = await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:6]}")
        integ = OCIntegration(
            base_url="https://oc.example",
            api_token_encrypted="enc",
            token_preview="prev",
            is_active=True,
        )
        db.add(integ)
        await db.flush()
        panel_b = OCPanel(
            integration_id=integ.id,
            source_panel_id=str(uuid.uuid4().int)[:8],
            purchaser_identity="buyer",
            name="Panel B",
            tenant_id=tenant_id,
            workspace_id=admin_b.workspace_id,
            oc_account_id=200,
        )
        db.add(panel_b)
        await db.flush()
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await bind_op.create_binding(
                db,
                user_a.id,
                UserPanelBindingCreate(oc_panel_id=panel_b.id),
                details_a,
            )
            raise AssertionError("cross-workspace panel binding must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_all_scope_lists_only_own_workspace():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        ua = await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:6]}")
        await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        resp = await user_op.get_users(db, details_a, UserListQuery())
        ids = {u.id for u in resp.users}
        assert ua.id in ids
        assert resp.total == len(resp.users)
        assert all(not u.username.startswith("ub_") for u in resp.users)
        await db.rollback()


async def test_bulk_expire_dry_run_does_not_count_other_workspace():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        dry = await user_op.bulk_modify_expire(
            db,
            BulkUser(amount=3600, dry_run=True, users=[user_b.id]),
            details_a,
        )
        assert dry.affected_users == 0
        await db.rollback()


async def test_hwid_cross_workspace_denied():
    hwid_op = HWIDOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await hwid_op.get_user_hwids(db, user_b.id, details_a)
            raise AssertionError("HWID cross-workspace must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_search_does_not_leak_other_workspace():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        secret = f"leak_marker_{uuid.uuid4().hex[:8]}"
        await _user(db, admin_b, secret)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        resp = await user_op.get_users(db, details_a, UserListQuery(search=secret))
        assert resp.total == 0
        assert resp.users == []
        await db.rollback()


async def run_all():
    await test_admin_cannot_list_other_workspace_users()
    await test_admin_cannot_get_other_workspace_user_by_id()
    await test_operator_inherits_workspace_user_access()
    await test_set_owner_cannot_cross_workspace()
    await test_panel_binding_cross_workspace_denied()
    await test_all_scope_lists_only_own_workspace()
    await test_bulk_expire_dry_run_does_not_count_other_workspace()
    await test_hwid_cross_workspace_denied()
    await test_search_does_not_leak_other_workspace()
    print("test_phase2_user_workspace: OK")


if __name__ == "__main__":
    asyncio.run(run_all())
