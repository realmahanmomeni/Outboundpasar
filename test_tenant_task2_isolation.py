"""
Task 2 tenant resource isolation — executable integration tests.
Creates isolated records and cleans them up on success or failure.
"""
import asyncio
import datetime
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, get_admin_by_id
from app.db.crud.user import create_user
from app.db.models import (
    Admin,
    AdminRole,
    Group,
    ProxyHost,
    Tenant,
    TenantStatus,
    User,
    inbounds_groups_association,
    users_groups_association,
)
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig
from app.models.admin import AdminModify
from app.models.group import GroupCreate, GroupModify
from app.models.host import CreateHost
from app.models.admin import AdminListQuery
from app.models.user import BulkUsersSelection, UserCreate, UserModify
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.operation.group import GroupOperation
from app.operation.host import HostOperation
from app.operation.user import UserOperation
from app.routers.panel import (
    PanelUpdate,
    get_panel,
    update_panel,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def _expect_not_found(coro_factory):
    try:
        await coro_factory()
        raise AssertionError("Expected HTTPException (not found / forbidden)")
    except HTTPException as exc:
        assert exc.status_code in (403, 404), f"Unexpected status {exc.status_code}: {exc.detail}"


async def run_tests():
    print("=== Task 2 Tenant Resource Isolation Tests ===\n")
    user_op = UserOperation(OperatorType.API)
    group_op = GroupOperation(OperatorType.API)
    host_op = HostOperation(OperatorType.API)
    admin_op = AdminOperation(OperatorType.API)

    tenant_a_id = tenant_b_id = None
    admin_a_id = admin_b_id = None
    op_a_id = op_b_id = None
    ids = {"user_a": None, "user_b": None, "user_op_a": None}
    group_a_id = group_b_id = None
    host_a_id = host_b_id = None
    panel_a_id = panel_b_id = None
    integration_id = None

    async with GetDB() as db:
        owner_row = (
            await db.execute(select(Admin).join(AdminRole).where(AdminRole.is_owner.is_(True)).limit(1))
        ).scalar_one()
        owner = build_admin_details(await get_admin_by_id(db, owner_row.id, load_role=True))

        role_admin = (await db.execute(select(AdminRole).where(AdminRole.id == 2))).scalar_one()
        role_operator = (await db.execute(select(AdminRole).where(AdminRole.id == 3))).scalar_one()

        tenant_a = Tenant(name=_uid("tenant_a"), status=TenantStatus.active)
        tenant_b = Tenant(name=_uid("tenant_b"), status=TenantStatus.active)
        db.add_all([tenant_a, tenant_b])
        await db.flush()
        tenant_a_id, tenant_b_id = tenant_a.id, tenant_b.id

        pwd = "testpwd123"
        from app.models.admin import hash_password

        hashed = await hash_password(pwd)
        admin_a = Admin(
            username=_uid("adm_a"),
            hashed_password=hashed,
            tenant_id=tenant_a.id,
            role_id=role_admin.id,
        )
        admin_b = Admin(
            username=_uid("adm_b"),
            hashed_password=hashed,
            tenant_id=tenant_b.id,
            role_id=role_admin.id,
        )
        op_a = Admin(
            username=_uid("op_a"),
            hashed_password=hashed,
            tenant_id=tenant_a.id,
            role_id=role_operator.id,
        )
        op_b = Admin(
            username=_uid("op_b"),
            hashed_password=hashed,
            tenant_id=tenant_b.id,
            role_id=role_operator.id,
        )
        db.add_all([admin_a, admin_b, op_a, op_b])
        await db.flush()
        admin_a_id, admin_b_id = admin_a.id, admin_b.id
        op_a_id, op_b_id = op_a.id, op_b.id
        await db.commit()

        details_a = build_admin_details(await get_admin_by_id(db, admin_a.id, load_role=True))
        details_b = build_admin_details(await get_admin_by_id(db, admin_b.id, load_role=True))
        op_details_a = build_admin_details(await get_admin_by_id(db, op_a.id, load_role=True))
        op_details_b = build_admin_details(await get_admin_by_id(db, op_b.id, load_role=True))

        inbound_tag = "VLESS TCP REALITY"
        with patch.object(group_op, "check_inbound_tags", new=AsyncMock()), patch.object(
            host_op, "check_host_inbound_tags", new=AsyncMock()
        ), patch.object(host_op, "validate_ds_host", new=AsyncMock(return_value=None)), patch.object(
            host_op, "validate_subscription_templates", new=AsyncMock()
        ), patch("app.operation.host.host_manager.add_host", new=AsyncMock()), patch(
            "app.operation.host.host_manager.add_hosts", new=AsyncMock()
        ), patch("app.operation.host.host_manager.remove_host", new=AsyncMock()):
            # --- Groups ---
            print("--- Groups ---")
            g_a = await group_op.create_group(
                db, GroupCreate(name=_uid("grp_a")[:16], inbound_tags=[inbound_tag]), details_a
            )
            g_b = await group_op.create_group(
                db, GroupCreate(name=_uid("grp_b")[:16], inbound_tags=[inbound_tag]), details_b
            )
            group_a_id, group_b_id = g_a.id, g_b.id
            assert (await db.get(Group, group_a_id)).tenant_id == tenant_a_id
            assert (await db.get(Group, group_b_id)).tenant_id == tenant_b_id

            got_a = await group_op._get_group_with_access(db, group_a_id, details_a)
            assert got_a.id == group_a_id
            await _expect_not_found(lambda: group_op._get_group_with_access(db, group_b_id, details_a))

            orig_b_name = g_b.name
            await group_op.modify_group(db, group_b_id, GroupModify(name=orig_b_name + "x"), details_b)
            await _expect_not_found(
                lambda: group_op.modify_group(db, group_b_id, GroupModify(name="hacked"), details_a)
            )
            assert (await db.get(Group, group_b_id)).name == orig_b_name + "x"

            # --- Users ---
            print("--- Users ---")
            u_a = await create_user(
                db,
                UserCreate(username=_uid("usr_a"), group_ids=[group_a_id]),
                groups=[await db.get(Group, group_a_id)],
                admin=admin_a,
            )
            u_b = await create_user(
                db,
                UserCreate(username=_uid("usr_b"), group_ids=[group_b_id]),
                groups=[await db.get(Group, group_b_id)],
                admin=admin_b,
            )
            await db.commit()
            ids["user_a"], ids["user_b"] = u_a.id, u_b.id

            ua = await user_op.get_user_by_id(db, ids["user_a"], details_a)
            ub = await user_op.get_user_by_id(db, ids["user_b"], details_b)
            assert ua.username == u_a.username
            assert ub.username == u_b.username
            await _expect_not_found(lambda: user_op.get_user_by_id(db, ids["user_b"], details_a))
            await _expect_not_found(lambda: user_op.get_user_by_id(db, ids["user_a"], details_b))

            await user_op.modify_user_by_id(db, ids["user_b"], UserModify(note="tenant-b-note"), details_b)
            await _expect_not_found(
                lambda: user_op.modify_user_by_id(db, ids["user_b"], UserModify(note="evil"), details_a)
            )
            assert (await user_op.get_user_by_id(db, ids["user_b"], details_b)).note == "tenant-b-note"

            # bulk remove cross-tenant
            await _expect_not_found(
                lambda: user_op.bulk_remove_users(db, BulkUsersSelection(ids={ids["user_b"]}), details_a)
            )
            assert await user_op.get_user_by_id(db, ids["user_b"], details_b)

            # --- Hosts ---
            print("--- Hosts ---")
            host_kwargs = {"priority": 0, "inbound_tag": inbound_tag, "port": 443}
            h_a = await host_op.create_host(
                db,
                CreateHost(remark="host-a", address=["1.2.3.4"], **host_kwargs),
                details_a,
            )
            h_b = await host_op.create_host(
                db,
                CreateHost(remark="host-b", address=["5.6.7.8"], **host_kwargs),
                details_b,
            )
            host_a_id, host_b_id = h_a.id, h_b.id
            assert (await db.get(ProxyHost, host_a_id)).tenant_id == tenant_a_id
            assert (await db.get(ProxyHost, host_b_id)).tenant_id == tenant_b_id

            assert (await host_op.get_validated_host(db, host_a_id, admin=details_a)).id == host_a_id
            await _expect_not_found(lambda: host_op.get_validated_host(db, host_b_id, admin=details_a))

            remark_b = (await db.get(ProxyHost, host_b_id)).remark
            await host_op.modify_host(
                db,
                host_b_id,
                CreateHost(remark=remark_b + "-ok", address=["5.6.7.8"], **host_kwargs),
                details_b,
            )
            await _expect_not_found(
                lambda: host_op.modify_host(
                    db,
                    host_b_id,
                    CreateHost(remark="hacked", address=["9.9.9.9"], **host_kwargs),
                    details_a,
                )
            )
            assert "hacked" not in (await db.get(ProxyHost, host_b_id)).remark

            # --- Panels ---
            print("--- Panels ---")
            intg = OCIntegration(
                base_url="http://task2-test",
                api_token_encrypted="enc",
                token_preview="t2",
                is_active=False,
            )
            db.add(intg)
            await db.flush()
            integration_id = intg.id

            panel_a = OCPanel(
                integration_id=intg.id,
                source_panel_id=_uid("src_a"),
                purchaser_identity=admin_a.username,
                tenant_id=tenant_a.id,
                name="Panel A",
            )
            panel_b = OCPanel(
                integration_id=intg.id,
                source_panel_id=_uid("src_b"),
                purchaser_identity=admin_b.username,
                tenant_id=tenant_b.id,
                name="Panel B",
            )
            db.add_all([panel_a, panel_b])
            await db.flush()
            panel_a_id, panel_b_id = panel_a.id, panel_b.id
            await db.commit()

            ctx_a = (admin_a.username, False, details_a)
            ctx_b = (admin_b.username, False, details_b)
            await get_panel(panel_id=panel_a_id, db=db, user_context=ctx_a)
            await _expect_not_found(lambda: get_panel(panel_id=panel_b_id, db=db, user_context=ctx_a))
            async def _cross_update_panel():
                await update_panel(
                    panel_id=panel_b_id,
                    update_data=PanelUpdate(multiplier=Decimal("9.99")),
                    db=db,
                    user_context=ctx_a,
                )

            await _expect_not_found(_cross_update_panel)

            # --- Admins ---
            print("--- Admins ---")
            listed = await admin_op.get_admins(db, AdminListQuery(), details_a)
            listed_ids = {a.id for a in listed.admins}
            assert admin_a_id in listed_ids
            assert admin_b_id not in listed_ids
            await _expect_not_found(
                lambda: admin_op.modify_admin_by_id(
                    db, admin_b_id, AdminModify(note="x"), details_a
                )
            )
            await _expect_not_found(
                lambda: admin_op.remove_admin_by_id(db, admin_b_id, current_admin=details_a)
            )

            # --- Operator (tenant-scoped, native OWN-scope RBAC preserved) ---
            print("--- Operators ---")
            u_op_a = await create_user(
                db,
                UserCreate(username=_uid("usr_op_a"), group_ids=[group_a_id]),
                groups=[await db.get(Group, group_a_id)],
                admin=op_a,
            )
            await db.commit()
            await user_op.get_user_by_id(db, u_op_a.id, op_details_a)
            await _expect_not_found(lambda: user_op.get_user_by_id(db, ids["user_b"], op_details_a))
            await _expect_not_found(lambda: user_op.get_user_by_id(db, ids["user_a"], op_details_a))
            ids["user_op_a"] = u_op_a.id

            # --- Owner global ---
            print("--- Owner ---")
            await user_op.get_user_by_id(db, ids["user_a"], owner)
            await user_op.get_user_by_id(db, ids["user_b"], owner)
            await host_op.get_validated_host(db, host_a_id, admin=owner)
            await host_op.get_validated_host(db, host_b_id, admin=owner)

        # cleanup
        print("\n--- Cleanup test records ---")
        async with GetDB() as db:
            if panel_a_id or panel_b_id:
                await db.execute(delete(OCPanelConfig).where(OCPanelConfig.panel_id.in_([panel_a_id, panel_b_id])))
                await db.execute(delete(OCPanel).where(OCPanel.id.in_([panel_a_id, panel_b_id])))
            if integration_id:
                await db.execute(delete(OCIntegration).where(OCIntegration.id == integration_id))
            user_ids = [i for i in ids.values() if i]
            if user_ids:
                await db.execute(
                    delete(users_groups_association).where(users_groups_association.c.user_id.in_(user_ids))
                )
                await db.execute(delete(User).where(User.id.in_(user_ids)))
            if host_a_id or host_b_id:
                await db.execute(delete(ProxyHost).where(ProxyHost.id.in_([host_a_id, host_b_id])))
            if group_a_id or group_b_id:
                gids = [group_a_id, group_b_id]
                await db.execute(
                    delete(inbounds_groups_association).where(inbounds_groups_association.c.group_id.in_(gids))
                )
                await db.execute(delete(Group).where(Group.id.in_(gids)))
            for aid in [admin_a_id, admin_b_id, op_a_id, op_b_id]:
                if aid:
                    await db.execute(delete(Admin).where(Admin.id == aid))
            if tenant_a_id or tenant_b_id:
                await db.execute(delete(Tenant).where(Tenant.id.in_([tenant_a_id, tenant_b_id])))
            await db.commit()

        print("\nALL TASK 2 ISOLATION TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(run_tests())
