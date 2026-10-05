"""Phase 9: cross-subsystem workspace isolation (HTTP operations, bindings, bulk, RBAC)."""
from __future__ import annotations

import asyncio
import uuid
from test_workspace_isolation_util import unique_username, unique_tag

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, load_admin_attrs
from app.db.crud.group import create_group as crud_create_group
from app.db.crud.user import create_user
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Group, Tenant, TenantStatus
from app.db.models_oc import OCIntegration, OCPanel
from app.models.admin import AdminDetails, AdminModify
from app.models.group import BulkGroup, GroupCreate
from app.models.user import BulkUsersSelection, UserCreate, UserModify
from app.models.user_panel_binding import UserPanelBindingCreate
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.operation.group import GroupOperation
from app.operation.host import HostOperation
from app.operation.user import UserOperation
from app.operation.user_panel_binding import UserPanelBindingOperation
from app.services.tenant_admin_scope import BUILTIN_ADMINISTRATOR_ROLE_ID, BUILTIN_OPERATOR_ROLE_ID


async def _tenant(db) -> int:
    t = Tenant(name=f"p9_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
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
    await load_admin_attrs(row, load_role=True, load_users=False, load_usage_logs=False)
    return row


async def _integration(db) -> OCIntegration:
    integ = OCIntegration(
        base_url="https://oc.example",
        api_token_encrypted="enc",
        token_preview="prev",
        is_active=True,
    )
    db.add(integ)
    await db.flush()
    return integ


async def _panel(db, integ: OCIntegration, tenant_id: int, workspace_id: int) -> OCPanel:
    p = OCPanel(
        integration_id=integ.id,
        source_panel_id=str(uuid.uuid4().int)[:10],
        purchaser_identity="1",
        name="panel",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        oc_account_id=1,
    )
    db.add(p)
    await db.flush()
    return p


async def _group(db, tenant_id: int, workspace_id: int, name: str) -> Group:
    return await crud_create_group(
        db,
        GroupCreate(name=name, inbound_tags=["VLESS TCP REALITY"]),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )


async def test_user_panel_binding_rejects_cross_workspace_panel():
    bind_op = UserPanelBindingOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
        integ = await _integration(db)
        panel_b = await _panel(db, integ, tenant_id, admin_b.workspace_id)
        user_a = await create_user(
            db,
            UserCreate(username=f"u_{uuid.uuid4().hex[:6]}", proxy_settings={}),
            groups=[],
            admin=op_a,
        )
        op_details = build_admin_details(op_a, include_loaded_metrics=False)
        payload = UserPanelBindingCreate(oc_panel_id=panel_b.id)
        try:
            await bind_op.create_binding(db, user_a.id, payload, op_details)
            raise AssertionError("User A + Panel B binding must be denied")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_bulk_users_rejects_mixed_workspace_ids():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
        user_a = await create_user(
            db,
            UserCreate(username=f"ua_{uuid.uuid4().hex[:6]}", proxy_settings={}),
            groups=[],
            admin=op_a,
        )
        user_b = await create_user(
            db,
            UserCreate(username=f"ub_{uuid.uuid4().hex[:6]}", proxy_settings={}),
            groups=[],
            admin=admin_b,
        )
        op_details = build_admin_details(op_a, include_loaded_metrics=False)
        try:
            await user_op.bulk_disable_users(
                db, BulkUsersSelection(ids={user_a.id, user_b.id}), op_details
            )
            raise AssertionError("bulk with foreign workspace user id must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_user_modify_rejects_foreign_workspace_group():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
        group_b = await _group(db, tenant_id, admin_b.workspace_id, f"gb_{uuid.uuid4().hex[:4]}")
        user_a = await create_user(
            db,
            UserCreate(username=f"ua_{uuid.uuid4().hex[:6]}", proxy_settings={}),
            groups=[],
            admin=op_a,
        )
        op_details = build_admin_details(op_a, include_loaded_metrics=False)
        try:
            await user_op.modify_user_by_id(
                db,
                user_a.id,
                UserModify(group_ids=[group_b.id]),
                op_details,
            )
            raise AssertionError("assigning foreign workspace group must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_bulk_group_scope_rejects_foreign_workspace_group():
    group_op = GroupOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
        group_b = await _group(db, tenant_id, admin_b.workspace_id, f"gb_{uuid.uuid4().hex[:4]}")
        user_a = await create_user(
            db,
            UserCreate(username=f"ua_{uuid.uuid4().hex[:6]}", proxy_settings={}),
            groups=[],
            admin=op_a,
        )
        op_details = build_admin_details(op_a, include_loaded_metrics=False)
        bulk = BulkGroup(usernames=[user_a.username], group_ids=[group_b.id])
        try:
            await group_op.bulk_add_groups(db, bulk, op_details)
            raise AssertionError("bulk add foreign workspace group must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_administrator_cannot_promote_operator_to_administrator_role():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, f"op_{uuid.uuid4().hex[:8]}")
        await load_admin_attrs(admin_a, load_role=True, load_users=False, load_usage_logs=False)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await admin_op.modify_admin_by_id(
                db,
                op_a.id,
                AdminModify(role_id=BUILTIN_ADMINISTRATOR_ROLE_ID),
                details_a,
            )
            raise AssertionError("operator → administrator promotion must be denied")
        except HTTPException as exc:
            assert exc.status_code == 403
        await db.rollback()


async def test_host_lookup_denies_cross_workspace():
    host_op = HostOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        from app.db.crud.host import create_host as crud_create_host
        from app.models.host import CreateHost

        tag = f"tag_{uuid.uuid4().hex[:8]}"
        host_b = await crud_create_host(
            db,
            CreateHost(
                remark=f"h_{uuid.uuid4().hex[:6]}",
                address=["2.0.0.2"],
                priority=0,
                inbound_tag=tag,
                port=443,
            ),
            tenant_id=tenant_id,
            workspace_id=admin_b.workspace_id,
        )
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await host_op.get_validated_host(db, host_b.id, admin=details_a)
            raise AssertionError("cross-workspace host must not resolve")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def run_all():
    await test_user_panel_binding_rejects_cross_workspace_panel()
    await test_bulk_users_rejects_mixed_workspace_ids()
    await test_user_modify_rejects_foreign_workspace_group()
    await test_bulk_group_scope_rejects_foreign_workspace_group()
    await test_administrator_cannot_promote_operator_to_administrator_role()
    await test_host_lookup_denies_cross_workspace()
    print("test_phase9_cross_system_workspace: 6 passed")


if __name__ == "__main__":
    asyncio.run(run_all())
