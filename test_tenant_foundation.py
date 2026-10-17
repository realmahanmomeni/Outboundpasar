import asyncio
import uuid
from fastapi import HTTPException
from sqlalchemy.sql.expression import select

from app.db import GetDB
from app.db.models import Admin, Tenant, AdminStatus, TenantStatus
from app.models.admin import AdminDetails, AdminRoleData
from app.routers.authentication import get_tenant_context

async def test_tenant_can_exist():
    print("Testing Tenant Foundation...")
    async with GetDB() as db:
        tenant = Tenant(name="Test Tenant", status=TenantStatus.active)
        db.add(tenant)
        await db.commit()
        
        saved = await db.execute(select(Tenant).where(Tenant.id == tenant.id))
        assert saved.scalar_one().name == "Test Tenant"
        print("  [x] Tenant creation and retrieval successful.")

async def test_admin_belongs_to_tenant():
    async with GetDB() as db:
        tenant = Tenant(name="Test Tenant Admin", status=TenantStatus.active)
        db.add(tenant)
        await db.commit()
        
        from app.db.models import AdminRole
        role = (await db.execute(select(AdminRole).limit(1))).scalar_one()
        
        admin = Admin(
            username=f"tenant_admin_test_{uuid.uuid4().hex[:8]}",
            hashed_password="pwd",
            tenant_id=tenant.id,
            role_id=role.id,
        )
        db.add(admin)
        await db.commit()
        
        saved = await db.execute(select(Admin).where(Admin.id == admin.id))
        assert saved.scalar_one().tenant_id == tenant.id
        print("  [x] Admin linked to Tenant successfully.")
        await db.delete(admin)
        await db.delete(tenant)
        await db.commit()

async def test_tenant_context_owner_is_global():
    admin = AdminDetails(username="owner", id=1, role=AdminRoleData(is_owner=True))
    ctx = await get_tenant_context(admin)
    assert ctx.is_owner is True
    assert ctx.tenant_id is None
    print("  [x] Owner context is global (no tenant restriction).")

async def test_tenant_context_non_owner_must_have_tenant():
    admin = AdminDetails(username="operator", id=2, role=AdminRoleData(is_owner=False))
    admin.tenant_id = None
    try:
        await get_tenant_context(admin)
        assert False, "Should have raised HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "must belong to a tenant" in exc.detail
        print("  [x] Non-owner without tenant correctly rejected (403).")

async def test_tenant_context_non_owner_with_tenant():
    admin = AdminDetails(username="operator2", id=3, role=AdminRoleData(is_owner=False))
    admin.tenant_id = 99
    ctx = await get_tenant_context(admin)
    assert ctx.is_owner is False
    assert ctx.tenant_id == 99
    print("  [x] Non-owner with tenant correctly resolved context.")

async def main():
    await test_tenant_can_exist()
    await test_admin_belongs_to_tenant()
    await test_tenant_context_owner_is_global()
    await test_tenant_context_non_owner_must_have_tenant()
    await test_tenant_context_non_owner_with_tenant()
    print("\nALL TENANT FOUNDATION TESTS PASSED.")

if __name__ == "__main__":
    asyncio.run(main())
