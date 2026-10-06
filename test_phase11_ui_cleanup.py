"""Phase 11 UI cleanup: group host picker, admin role options policy, admin create response."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from app.db import GetDB
from app.services.assignable_hosts import list_group_host_options
from app.services.tenant_admin_scope import BUILTIN_ADMINISTRATOR_ROLE_ID, assert_tenant_admin_may_assign_role
from app.models.admin import AdminDetails, AdminRoleData, AdminStatus


@pytest.mark.asyncio
async def test_group_host_options_exclude_core_inbound_tags():
    core_only_tag = f"core_only_{uuid.uuid4().hex[:10]}"
    async with GetDB() as db:
        with patch("app.core.manager.core_manager.get_inbounds", new_callable=AsyncMock) as mock_inbounds:
            mock_inbounds.return_value = [core_only_tag]
            options = await list_group_host_options(db)
        tags = {o.inbound_tag for o in options}
        assert core_only_tag not in tags


@pytest.mark.asyncio
async def test_owner_may_assign_administrator_role():
    async with GetDB() as db:
        owner = AdminDetails(
            id=1,
            username="owner",
            is_owner=True,
            status=AdminStatus.active,
            role=AdminRoleData(id=1, name="owner", is_owner=True),
        )
        await assert_tenant_admin_may_assign_role(db, owner, BUILTIN_ADMINISTRATOR_ROLE_ID)


@pytest.mark.asyncio
async def test_tenant_administrator_cannot_assign_administrator_role():
    async with GetDB() as db:
        tenant_admin = AdminDetails(
            id=2,
            username="tenant_admin",
            tenant_id=10,
            status=AdminStatus.active,
            is_sudo=False,
            role=AdminRoleData(id=BUILTIN_ADMINISTRATOR_ROLE_ID, name="administrator", is_owner=False),
        )
        try:
            await assert_tenant_admin_may_assign_role(db, tenant_admin, BUILTIN_ADMINISTRATOR_ROLE_ID)
            raise AssertionError("expected 403")
        except HTTPException as exc:
            assert exc.status_code == 403


async def main():
    await test_group_host_options_exclude_core_inbound_tags()
    await test_owner_may_assign_administrator_role()
    await test_tenant_administrator_cannot_assign_administrator_role()
    print("test_phase11_ui_cleanup: OK")


if __name__ == "__main__":
    asyncio.run(main())
