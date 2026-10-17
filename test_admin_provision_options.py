"""Filtered admin provision lists must not expose all integration junk tenants."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.db import GetDB
from app.db.models import Admin, Tenant, TenantStatus, Workspace
from app.services.admin_provision import list_provision_tenants, list_provision_workspaces


@pytest.mark.asyncio
async def test_provision_tenants_excludes_integration_junk_without_admin_workspace():
    async with GetDB() as db:
        junk = Tenant(name="Integration (owner-test)", status=TenantStatus.active)
        orphan = Tenant(name=f"Org-{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(junk)
        db.add(orphan)
        await db.flush()

        all_count = await db.scalar(select(func.count()).select_from(Tenant))
        listed = await list_provision_tenants(db)
        listed_ids = {tid for tid, _ in listed}
        assert junk.id not in listed_ids
        assert orphan.id not in listed_ids
        assert len(listed) < (all_count or 0)

        await db.rollback()


@pytest.mark.asyncio
async def test_provision_workspaces_lists_administrator_workspaces():
    async with GetDB() as db:
        admin = (
            await db.execute(select(Admin).where(Admin.workspace_id.is_not(None)).limit(1))
        ).scalar_one()
        rows = await list_provision_workspaces(db)
        assert any(wid == admin.workspace_id for wid, *_ in rows)
