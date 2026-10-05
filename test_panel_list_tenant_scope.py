"""Regression: tenant-scoped panel list must not 500 for administrators without tenant_id."""
from __future__ import annotations

import asyncio

from fastapi import HTTPException

from app.db import GetDB
from app.models.admin import AdminDetails, AdminRoleData
from app.routers.panel import list_panels


async def test_list_panels_rejects_admin_without_tenant_scope():
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


async def main():
    await test_list_panels_rejects_admin_without_tenant_scope()
    print("test_panel_list_tenant_scope: OK")


if __name__ == "__main__":
    asyncio.run(main())
