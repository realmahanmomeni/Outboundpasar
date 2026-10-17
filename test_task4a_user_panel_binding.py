"""
Task 4A — explicit UserPanelBinding API regression tests.
"""
import asyncio
import datetime
import uuid

if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, get_admin_by_id
from app.db.crud.user import create_user
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User
from app.db.models_oc import OCIntegration, OCPanel, OCUserMapping, UserPanelBinding
from app.models.user import UserCreate
from app.models.user_panel_binding import UserPanelBindingCreate
from app.operation import OperatorType
from app.operation.user_panel_binding import UserPanelBindingOperation


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def _expect_http(status_codes: tuple[int, ...], coro_factory):
    try:
        await coro_factory()
        raise AssertionError(f"Expected HTTPException {status_codes}")
    except HTTPException as exc:
        assert exc.status_code in status_codes, f"Unexpected {exc.status_code}: {exc.detail}"


async def run_tests():
    print("=== Task 4A User Panel Binding Tests ===\n")
    op = UserPanelBindingOperation(OperatorType.API)

    tenant_a_id = tenant_b_id = None
    admin_a_id = admin_b_id = None
    owner_details = None
    admin_a_details = admin_b_details = None
    user_a_id = user_b_id = None
    panel_a_id = panel_b_id = None
    integration_id = None
    mapping_a_id = None

    async with GetDB() as db:
        owner_row = (
            await db.execute(select(Admin).join(AdminRole).where(AdminRole.is_owner.is_(True)).limit(1))
        ).scalar_one()
        owner_details = build_admin_details(await get_admin_by_id(db, owner_row.id, load_role=True))

        role_admin = (await db.execute(select(AdminRole).where(AdminRole.id == 2))).scalar_one()

        tenant_a = Tenant(name=_uid("t4a_a"), status=TenantStatus.active)
        tenant_b = Tenant(name=_uid("t4a_b"), status=TenantStatus.active)
        db.add_all([tenant_a, tenant_b])
        await db.flush()
        tenant_a_id, tenant_b_id = tenant_a.id, tenant_b.id

        from app.models.admin import hash_password

        hashed = await hash_password("testpwd123")
        admin_a = Admin(
            username=_uid("adm4a_a"),
            hashed_password=hashed,
            tenant_id=tenant_a.id,
            role_id=role_admin.id,
        )
        admin_b = Admin(
            username=_uid("adm4a_b"),
            hashed_password=hashed,
            tenant_id=tenant_b.id,
            role_id=role_admin.id,
        )
        db.add_all([admin_a, admin_b])
        await db.flush()
        admin_a_id, admin_b_id = admin_a.id, admin_b.id
        admin_a_details = build_admin_details(await get_admin_by_id(db, admin_a.id, load_role=True))

        user_a = await create_user(
            db,
            UserCreate(username=_uid("usr4a_a"), proxy_settings={}),
            groups=[],
            admin=admin_a,
        )
        user_b = await create_user(
            db,
            UserCreate(username=_uid("usr4a_b"), proxy_settings={}),
            groups=[],
            admin=admin_b,
        )
        user_a_id, user_b_id = user_a.id, user_b.id

        integration = OCIntegration(
            base_url="https://oc.example.test",
            api_token_encrypted="enc",
            token_preview="prev",
        )
        db.add(integration)
        await db.flush()
        integration_id = integration.id

        panel_a = OCPanel(
            integration_id=integration.id,
            source_panel_id=_uid("sp_a"),
            purchaser_identity="buyer",
            name="Panel A",
            tenant_id=tenant_a.id,
        )
        panel_b = OCPanel(
            integration_id=integration.id,
            source_panel_id=_uid("sp_b"),
            purchaser_identity="buyer",
            name="Panel B",
            tenant_id=tenant_b.id,
        )
        db.add_all([panel_a, panel_b])
        await db.flush()
        panel_a_id, panel_b_id = panel_a.id, panel_b.id

        mapping_a = OCUserMapping(
            user_id=user_a.id,
            panel_id=panel_a.id,
            external_user_id=str(uuid.uuid4()),
            status="active",
        )
        db.add(mapping_a)
        await db.flush()
        mapping_a_id = mapping_a.id

        await db.commit()

    print("1. Tenant A admin cannot bind tenant B panel")
    async with GetDB() as db:
        await _expect_http(
            (404,),
            lambda: op.create_binding(
                db,
                user_a_id,
                UserPanelBindingCreate(oc_panel_id=panel_b_id),
                admin_a_details,
            ),
        )
        await db.rollback()
    print("   [x] Cross-tenant panel rejected\n")

    print("2. Duplicate binding prevented")
    async with GetDB() as db:
        first = await op.create_binding(
            db,
            user_a_id,
            UserPanelBindingCreate(oc_panel_id=panel_a_id),
            admin_a_details,
        )
        await db.commit()
        binding_id = first.id
    async with GetDB() as db:
        await _expect_http(
            (409,),
            lambda: op.create_binding(
                db,
                user_a_id,
                UserPanelBindingCreate(oc_panel_id=panel_a_id),
                admin_a_details,
            ),
        )
        await db.rollback()
    print("   [x] Duplicate returns 409\n")

    print("3. Owner access works across tenant-scoped user/panel")
    async with GetDB() as db:
        owner_binding = await op.create_binding(
            db,
            user_b_id,
            UserPanelBindingCreate(oc_panel_id=panel_b_id),
            owner_details,
        )
        listed = await op.list_bindings(db, user_b_id, owner_details)
        assert any(b.id == owner_binding.id for b in listed.bindings)
        await db.commit()
        owner_binding_id = owner_binding.id
    print("   [x] Owner create + list OK\n")

    print("4. Delete binding removes desired state only, not OC mapping")
    async with GetDB() as db:
        await op.delete_binding(db, user_a_id, binding_id, admin_a_details)
        mapping = (
            await db.execute(select(OCUserMapping).where(OCUserMapping.id == mapping_a_id))
        ).scalar_one_or_none()
        assert mapping is not None, "OCUserMapping should remain after binding delete"
        binding_row = (
            await db.execute(select(UserPanelBinding).where(UserPanelBinding.id == binding_id))
        ).scalar_one_or_none()
        assert binding_row is None
        await db.commit()
    print("   [x] Binding removed, OCUserMapping intact\n")

    print("=== All Task 4A tests passed ===\n")

    async with GetDB() as db:
        await db.execute(delete(UserPanelBinding).where(UserPanelBinding.user_id.in_([user_a_id, user_b_id])))
        await db.execute(delete(OCUserMapping).where(OCUserMapping.id == mapping_a_id))
        await db.execute(delete(User).where(User.id.in_([user_a_id, user_b_id])))
        await db.execute(delete(OCPanel).where(OCPanel.id.in_([panel_a_id, panel_b_id])))
        await db.execute(delete(OCIntegration).where(OCIntegration.id == integration_id))
        await db.execute(delete(Admin).where(Admin.id.in_([admin_a_id, admin_b_id])))
        await db.execute(delete(Tenant).where(Tenant.id.in_([tenant_a_id, tenant_b_id])))
        await db.commit()


if __name__ == "__main__":
    asyncio.run(run_tests())
