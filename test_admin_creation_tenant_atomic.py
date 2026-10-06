"""Owner admin creation must assign tenant atomically (no orphan admins)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.db import GetDB
from app.db.crud.admin import get_admin
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.models.admin import AdminCreate, AdminDetails, AdminRoleData
from app.operation import OperatorType
from app.operation.admin import AdminOperation


@pytest.mark.asyncio
async def test_owner_create_administrator_without_tenant_auto_provisions():
    owner = AdminDetails(
        id=1,
        username="owner",
        is_owner=True,
        role=AdminRoleData(id=1, name="owner", is_owner=True),
    )
    op = AdminOperation(operator_type=OperatorType.API)
    username = f"scoped_{uuid.uuid4().hex[:8]}"
    payload = AdminCreate(
        username=username,
        password="TestPass123!!",
        role_id=2,
        tenant_id=None,
    )
    async with GetDB() as db:
        await op.create_admin(db, payload, owner)
        row = await get_admin(db, username, load_users=False, load_usage_logs=False)
        assert row.tenant_id is not None
        assert row.workspace_id is not None
        await db.rollback()


@pytest.mark.asyncio
async def test_owner_create_administrator_with_tenant_has_workspace():
    owner = AdminDetails(
        id=1,
        username="owner",
        is_owner=True,
        role=AdminRoleData(id=1, name="owner", is_owner=True),
    )
    op = AdminOperation(operator_type=OperatorType.API)
    username = f"scoped_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant = Tenant(name=f"t_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.commit()
        await db.refresh(tenant)

        payload = AdminCreate(
            username=username,
            password="TestPass123!!",
            role_id=2,
            tenant_id=tenant.id,
        )
        await op.create_admin(db, payload, owner)

        row = await get_admin(db, username, load_users=False, load_usage_logs=False)
        assert row.tenant_id == tenant.id
        assert row.workspace_id is not None

        await db.rollback()
