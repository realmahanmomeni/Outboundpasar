"""Phase 7: operator identity, workspace inheritance, RBAC vs workspace, operator CRUD isolation."""
from __future__ import annotations

import asyncio
import uuid
from test_workspace_isolation_util import unique_username, unique_tag

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, load_admin_attrs
from app.db.crud.user import create_user
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User
from app.db.models_oc import TenantTelegramConnection, TenantTelegramConnectionStatus
from app.models.admin import AdminDetails, AdminListQuery, AdminRoleData, AdminSimpleListQuery, BulkAdminSelection
from app.models.user import UserCreate
from app.models.admin_role import RolePermissions, UsersPermissions
from app.operation.permissions import is_scope_all
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.operation.permissions import PermissionDenied, enforce_permission
from app.operation.user import UserOperation
from app.services.oc_actor_connection import resolve_oc_telegram_connection_for_actor
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID
from app.services.workspace_scope import resolve_admin_workspace_id


async def _tenant(db) -> int:
    t = Tenant(name=f"p7_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
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


def _operator_details(op_row: Admin) -> AdminDetails:
    return build_admin_details(op_row, include_loaded_metrics=False)


async def _operator(db, tenant_id: int, creator: Admin, name: str) -> Admin:
    op_role = (await db.execute(select(AdminRole).where(AdminRole.id == BUILTIN_OPERATOR_ROLE_ID))).scalar_one()
    row = Admin(username=name, hashed_password="x", role_id=op_role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    creator_details = build_admin_details(creator, include_loaded_metrics=False)
    await assign_workspace_for_new_admin(db, row, creator=creator_details)
    await db.refresh(row)
    await load_admin_attrs(row, load_users=False, load_usage_logs=False, load_role=True)
    return row


async def _user(db, owner: Admin, username: str) -> User:
    return await create_user(db, UserCreate(username=username, proxy_settings={}), groups=[], admin=owner)


async def test_operator_resolves_workspace_via_inherited_membership():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op_a"))
        op_b = await _operator(db, tenant_id, admin_b, unique_username("op_b"))
        op_a_details = build_admin_details(op_a, include_loaded_metrics=False)
        op_b_details = build_admin_details(op_b, include_loaded_metrics=False)
        ws_a = await resolve_admin_workspace_id(db, op_a_details, False)
        ws_b = await resolve_admin_workspace_id(db, op_b_details, False)
        assert ws_a == admin_a.workspace_id
        assert ws_b == admin_b.workspace_id
        assert ws_a != ws_b
        await db.rollback()


async def test_administrator_cannot_list_or_fetch_foreign_workspace_operators():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op_a"))
        op_b = await _operator(db, tenant_id, admin_b, unique_username("op_b"))
        await load_admin_attrs(admin_a, load_role=True, load_users=False, load_usage_logs=False)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)

        listed = await admin_op.get_admins(db, AdminListQuery(), details_a)
        usernames = {a.username for a in listed.admins}
        assert op_b.username not in usernames
        assert op_a.username in usernames

        simple_resp = await admin_op.get_admins_simple(
            db, AdminSimpleListQuery(search=op_b.username), details_a
        )
        assert simple_resp.total == 0
        assert not simple_resp.admins

        try:
            await admin_op.get_validated_admin_by_id(db, op_b.id, current_admin=details_a)
            raise AssertionError("foreign workspace operator id must not resolve")
        except HTTPException as exc:
            assert exc.status_code == 404

        await db.rollback()


async def test_bulk_admin_actions_respect_workspace():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_b = await _operator(db, tenant_id, admin_b, unique_username("op_b"))
        await load_admin_attrs(admin_a, load_role=True, load_users=False, load_usage_logs=False)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)

        try:
            await admin_op.bulk_set_admins_disabled(
                db, BulkAdminSelection(ids={op_b.id}), details_a, is_disabled=True
            )
            raise AssertionError("bulk action on foreign workspace operator must fail")
        except HTTPException as exc:
            assert exc.status_code == 404

        await db.rollback()


async def test_operator_telegram_inherits_workspace_administrator_binding():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op_a"))
        conn_a = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin_a.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=100,
        )
        conn_b = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin_b.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=200,
        )
        db.add_all([conn_a, conn_b])
        await db.flush()

        op_details = build_admin_details(op_a, include_loaded_metrics=False)
        resolved = await resolve_oc_telegram_connection_for_actor(db, op_details, False, tenant_id)
        assert resolved is not None and resolved.id == conn_a.id

        op_b = await _operator(db, tenant_id, admin_b, unique_username("op_b"))
        op_b_details = build_admin_details(op_b, include_loaded_metrics=False)
        resolved_b = await resolve_oc_telegram_connection_for_actor(db, op_b_details, False, tenant_id)
        assert resolved_b is not None and resolved_b.id == conn_b.id
        assert resolved_b.id != resolved.id

        await db.rollback()


def _assert_denied(details: AdminDetails, resource: str, action: str) -> None:
    try:
        enforce_permission(details, resource, action)
        raise AssertionError(f"operator must not have {resource}.{action}")
    except PermissionDenied:
        pass


def _assert_allowed(details: AdminDetails, resource: str, action: str) -> None:
    enforce_permission(details, resource, action)


async def test_builtin_operator_rbac_matches_admin_roles_seed():
    """Operator capabilities must match the builtin operator role from admin_roles (Admin Roles UI source)."""
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
        details = _operator_details(op_a)

        for action in ("create", "read", "read_simple", "update", "delete", "reset_usage", "revoke_sub", "activate_next_plan"):
            _assert_allowed(details, "users", action)
        _assert_denied(details, "users", "set_owner")
        assert not is_scope_all(details, "users", "read")

        _assert_allowed(details, "groups", "read_simple")
        for action in ("read", "create", "update", "delete"):
            _assert_denied(details, "groups", action)

        _assert_allowed(details, "templates", "read")
        _assert_allowed(details, "templates", "read_simple")
        for action in ("create", "update", "delete"):
            _assert_denied(details, "templates", action)

        _assert_allowed(details, "settings", "read_general")
        _assert_denied(details, "settings", "read")
        _assert_denied(details, "settings", "update")

        _assert_allowed(details, "system", "read")
        _assert_allowed(details, "hwids", "read")
        _assert_allowed(details, "hwids", "delete")

        _assert_allowed(details, "api_keys", "read")
        _assert_allowed(details, "api_keys", "read_simple")
        assert not is_scope_all(details, "api_keys", "read")

        for resource in ("admins", "nodes", "hosts", "client_templates", "cores", "admin_roles"):
            for action in ("read", "create", "update", "delete", "read_simple"):
                _assert_denied(details, resource, action)

        await db.rollback()


async def test_operator_panel_and_telegram_management_denied_by_rbac():
    """Panels/integration use nodes permissions; operators must not manage panels or OC Telegram."""
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
        details = _operator_details(op_a)

        for action in ("read", "read_simple", "update", "create", "delete", "reconnect", "update_core", "stats", "logs"):
            _assert_denied(details, "nodes", action)

        await db.rollback()


async def test_operator_allowed_user_ops_respect_workspace():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op_a"))
        user_a = await _user(db, op_a, f"ua_{uuid.uuid4().hex[:6]}")
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        details = _operator_details(op_a)

        _assert_allowed(details, "users", "read")
        got = await user_op.get_user_by_id(db, user_a.id, details)
        assert got.id == user_a.id

        try:
            await user_op.get_user_by_id(db, user_b.id, details)
            raise AssertionError("cross-workspace user must be denied")
        except HTTPException as exc:
            assert exc.status_code == 404

        await db.rollback()


async def test_rbac_and_workspace_are_independent():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op_a"))
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")

        read_only_role = AdminRoleData(
            id=BUILTIN_OPERATOR_ROLE_ID,
            name="operator",
            permissions=RolePermissions(users=UsersPermissions(read=True)),
        )
        op_details = build_admin_details(op_a, include_loaded_metrics=False)
        op_details.role = read_only_role

        enforce_permission(op_details, "users", "read")
        try:
            enforce_permission(op_details, "users", "create")
            raise AssertionError("RBAC must deny create without permission")
        except PermissionDenied:
            pass

        try:
            await user_op.get_user_by_id(db, user_b.id, op_details)
            raise AssertionError("workspace must deny cross-workspace read even with users.read")
        except HTTPException as exc:
            assert exc.status_code == 404

        await db.rollback()


async def run_all():
    await test_operator_resolves_workspace_via_inherited_membership()
    await test_administrator_cannot_list_or_fetch_foreign_workspace_operators()
    await test_bulk_admin_actions_respect_workspace()
    await test_operator_telegram_inherits_workspace_administrator_binding()
    await test_builtin_operator_rbac_matches_admin_roles_seed()
    await test_operator_panel_and_telegram_management_denied_by_rbac()
    await test_operator_allowed_user_ops_respect_workspace()
    await test_rbac_and_workspace_are_independent()
    print("test_phase7_operator_system_workspace: 8 passed")


if __name__ == "__main__":
    asyncio.run(run_all())
