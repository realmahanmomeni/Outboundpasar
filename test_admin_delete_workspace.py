"""Admin deletion must remove owned workspaces before admins (FK RESTRICT)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import get_admin
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, Workspace
from app.models.admin import AdminCreate, AdminDetails, AdminRoleData, BulkAdminSelection
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from test_integration_admin import owner_admin_details, seed_tenant_admin_workspace


@pytest.mark.asyncio
async def test_remove_admin_by_id_deletes_owned_workspace():
    owner = owner_admin_details()
    op = AdminOperation(operator_type=OperatorType.API)
    username = f"del_ws_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant_id, workspace_id, admin_id = await seed_tenant_admin_workspace(db, username=username)
        assert workspace_id is not None

        await op.remove_admin_by_id(db, admin_id, current_admin=owner)

        ws = await db.scalar(select(Workspace).where(Workspace.id == workspace_id))
        assert ws is None
        assert await get_admin(db, username, load_users=False, load_usage_logs=False) is None

        await db.rollback()


@pytest.mark.asyncio
async def test_bulk_remove_admin_deletes_owned_workspace():
    owner = owner_admin_details()
    op = AdminOperation(operator_type=OperatorType.API)
    username = f"bulk_del_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        _tenant_id, workspace_id, admin_id = await seed_tenant_admin_workspace(db, username=username)

        result = await op.bulk_remove_admins(db, BulkAdminSelection(ids=[admin_id]), admin=owner)
        assert result.count == 1
        assert username in result.admins

        ws = await db.scalar(select(Workspace).where(Workspace.id == workspace_id))
        assert ws is None

        await db.rollback()


@pytest.mark.asyncio
async def test_tenant_admin_cannot_delete_peer_admin_in_other_workspace():
    """Unauthorized cross-tenant admin delete remains forbidden."""
    from fastapi import HTTPException

    op = AdminOperation(operator_type=OperatorType.API)
    async with GetDB() as db:
        role = AdminRole(
            name=f"administrator_{uuid.uuid4().hex[:12]}",
            is_owner=False,
            permissions={},
            limits={},
            features={},
            access={},
            hwid={},
        )
        db.add(role)
        await db.flush()

        tenant_a = Tenant(name=f"a_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        tenant_b = Tenant(name=f"b_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add_all([tenant_a, tenant_b])
        await db.flush()

        admin_a = Admin(username=f"a_{uuid.uuid4().hex[:6]}", hashed_password="x", role_id=role.id, tenant_id=tenant_a.id)
        admin_b = Admin(username=f"b_{uuid.uuid4().hex[:6]}", hashed_password="x", role_id=role.id, tenant_id=tenant_b.id)
        db.add_all([admin_a, admin_b])
        await db.flush()

        ws_a = Workspace(tenant_id=tenant_a.id, owner_admin_id=admin_a.id)
        ws_b = Workspace(tenant_id=tenant_b.id, owner_admin_id=admin_b.id)
        db.add_all([ws_a, ws_b])
        await db.flush()
        admin_a.workspace_id = ws_a.id
        admin_b.workspace_id = ws_b.id
        await db.flush()

        caller = AdminDetails(
            id=admin_a.id,
            username=admin_a.username,
            tenant_id=tenant_a.id,
            workspace_id=ws_a.id,
            role=AdminRoleData(id=role.id, name=role.name, is_owner=False),
        )

        with pytest.raises(HTTPException) as exc_info:
            await op.bulk_remove_admins(db, BulkAdminSelection(ids=[admin_b.id]), admin=caller)
        assert exc_info.value.status_code in (403, 404)

        peer = await db.get(Admin, admin_b.id)
        assert peer is not None
        peer_ws = await db.scalar(select(Workspace).where(Workspace.owner_admin_id == admin_b.id))
        assert peer_ws is not None

        await db.rollback()
