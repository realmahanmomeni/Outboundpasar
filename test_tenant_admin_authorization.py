"""Tenant administrator scope, panel list access, and operator-only admin creation."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.db.crud.admin import build_admin_details, get_admin
from app.models.admin import AdminDetails, AdminRoleData, AdminStatus
from app.models.admin_role import CRUDPermissions, RolePermissions
from app.routers.panel import list_panels
from app.services.tenant_admin_scope import assert_tenant_admin_may_assign_role, resolve_admin_tenant_id


@pytest.mark.asyncio
async def test_list_panels_rejects_unknown_admin_without_tenant():
    admin_ctx = (
        "scoped_admin_test",
        False,
        AdminDetails(
            id=999999,
            username="scoped_admin_test",
            tenant_id=None,
            role=AdminRoleData(is_owner=False, permissions={"nodes": {"read": True}}),
        ),
    )
    async with GetDB() as db:
        try:
            await list_panels(db, admin_ctx)
        except HTTPException as exc:
            assert exc.status_code == 403
            assert "Tenant scope required" in str(exc.detail)
        else:
            raise AssertionError("expected 403 for administrator without tenant_id")


@pytest.mark.asyncio
async def test_resolve_admin_tenant_id_single_tenant_bind():
    async with GetDB() as db:
        tenant = Tenant(name=f"ta_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()

        other = Tenant(name=f"other_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(other)
        await db.flush()

        role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin_row = Admin(
            username=f"ta_admin_{uuid.uuid4().hex[:6]}",
            hashed_password="x",
            role_id=role.id,
            tenant_id=None,
        )
        db.add(admin_row)
        await db.flush()

        details = build_admin_details(admin_row, include_loaded_metrics=True)
        assert await resolve_admin_tenant_id(db, details, False, persist_single_tenant_bind=False) is None

        await db.rollback()

    async with GetDB() as db:
        active_tenants = (
            await db.execute(select(Tenant.id).where(Tenant.status == TenantStatus.active))
        ).scalars().all()
        if len(active_tenants) != 1:
            return

        single_id = active_tenants[0]
        role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin_row = Admin(
            username=f"ta_single_{uuid.uuid4().hex[:6]}",
            hashed_password="x",
            role_id=role.id,
            tenant_id=None,
        )
        db.add(admin_row)
        await db.flush()
        details = build_admin_details(admin_row, include_loaded_metrics=True)

        resolved = await resolve_admin_tenant_id(db, details, False)
        assert resolved == single_id
        await db.refresh(admin_row)
        assert admin_row.tenant_id == single_id

        panels = await list_panels(db, (details.username, False, details))
        assert panels == []

        await db.rollback()


@pytest.mark.asyncio
async def test_tenant_administrator_cannot_create_administrator_role():
    async with GetDB() as db:
        tenant_admin = AdminDetails(
            id=1,
            username="tenant_admin",
            tenant_id=10,
            status=AdminStatus.active,
            is_sudo=False,
            role=AdminRoleData(
                id=2,
                name="administrator",
                is_owner=False,
                permissions=RolePermissions(admins=CRUDPermissions(create=True)),
            ),
        )
        try:
            await assert_tenant_admin_may_assign_role(db, tenant_admin, 2)
            raise AssertionError("expected 403")
        except HTTPException as exc:
            assert exc.status_code == 403
            assert "administrator role" in exc.detail.lower()


@pytest.mark.asyncio
async def test_tenant_administrator_may_create_operator_role():
    async with GetDB() as db:
        tenant_admin = AdminDetails(
            id=1,
            username="tenant_admin",
            tenant_id=10,
            status=AdminStatus.active,
            is_sudo=False,
            role=AdminRoleData(
                id=2,
                name="administrator",
                is_owner=False,
            ),
        )
        await assert_tenant_admin_may_assign_role(db, tenant_admin, 3)


@pytest.mark.asyncio
async def test_operator_cannot_list_panels_without_nodes_read():
    async with GetDB() as db:
        role = (await db.execute(select(AdminRole).where(AdminRole.name == "operator"))).scalar_one()
        tenant = Tenant(name=f"op_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        op_admin = AdminDetails(
            id=2,
            username="operator_user",
            tenant_id=tenant.id,
            role=AdminRoleData(id=role.id, name=role.name, is_owner=False, permissions=role.permissions),
        )
        from app.operation.permissions import PermissionDenied, enforce_permission

        try:
            enforce_permission(op_admin, "nodes", "read")
            raise AssertionError("operator should not have nodes:read")
        except PermissionDenied:
            pass
        await db.rollback()


async def main():
    await test_list_panels_rejects_unknown_admin_without_tenant()
    await test_resolve_admin_tenant_id_single_tenant_bind()
    await test_tenant_administrator_cannot_create_administrator_role()
    await test_tenant_administrator_may_create_operator_role()
    await test_operator_cannot_list_panels_without_nodes_read()
    print("test_tenant_admin_authorization: OK")


if __name__ == "__main__":
    asyncio.run(main())
