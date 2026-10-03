"""
Regression tests for Task 2 audit fixes: expired-user tenant scope and nested host validation.
"""
import asyncio
import datetime
import uuid
from datetime import UTC, datetime as dt, timedelta as td
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
from app.models.host import CreateHost, TransportSettings, XHttpSettings
from app.models.user import ExpiredUsersQuery
from app.operation import OperatorType
from app.operation.group import GroupOperation
from app.operation.host import HostOperation
from app.operation.user import UserOperation
from app.models.user import UserCreate


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def run_tests():
    print("=== Task 2 Security Fix Regression Tests ===\n")
    user_op = UserOperation(OperatorType.API)
    group_op = GroupOperation(OperatorType.API)
    host_op = HostOperation(OperatorType.API)

    tenant_a_id = tenant_b_id = None
    admin_a_id = admin_b_id = None
    group_a_id = None
    expired_a = expired_b = None
    host_a_id = host_b_id = None

    async with GetDB() as db:
        owner_row = (
            await db.execute(select(Admin).join(AdminRole).where(AdminRole.is_owner.is_(True)).limit(1))
        ).scalar_one()
        owner = build_admin_details(await get_admin_by_id(db, owner_row.id, load_role=True))

        role_admin = (await db.execute(select(AdminRole).where(AdminRole.id == 2))).scalar_one()

        tenant_a = Tenant(name=_uid("fix_ta"), status=TenantStatus.active)
        tenant_b = Tenant(name=_uid("fix_tb"), status=TenantStatus.active)
        db.add_all([tenant_a, tenant_b])
        await db.flush()
        tenant_a_id, tenant_b_id = tenant_a.id, tenant_b.id

        from app.models.admin import hash_password

        hashed = await hash_password("testpwd123")
        admin_a = Admin(
            username=_uid("fix_adm_a"),
            hashed_password=hashed,
            tenant_id=tenant_a.id,
            role_id=role_admin.id,
        )
        admin_b = Admin(
            username=_uid("fix_adm_b"),
            hashed_password=hashed,
            tenant_id=tenant_b.id,
            role_id=role_admin.id,
        )
        db.add_all([admin_a, admin_b])
        await db.flush()
        admin_a_id, admin_b_id = admin_a.id, admin_b.id
        await db.commit()

        details_a = build_admin_details(await get_admin_by_id(db, admin_a.id, load_role=True))
        details_b = build_admin_details(await get_admin_by_id(db, admin_b.id, load_role=True))

        inbound_tag = "VLESS TCP REALITY"
        with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
            from app.models.group import GroupCreate

            g_a = await group_op.create_group(
                db, GroupCreate(name=_uid("fxgrp")[:16], inbound_tags=[inbound_tag]), details_a
            )
            group_a_id = g_a.id
            g_b = await group_op.create_group(
                db, GroupCreate(name=_uid("fxgrb")[:16], inbound_tags=[inbound_tag]), details_b
            )

        past = dt.now(UTC) - td(days=7)
        u_a = await create_user(
            db,
            UserCreate(username=_uid("exp_a"), group_ids=[group_a_id], expire=past),
            groups=[await db.get(Group, group_a_id)],
            admin=admin_a,
        )
        u_b = await create_user(
            db,
            UserCreate(username=_uid("exp_b"), group_ids=[g_b.id], expire=past),
            groups=[await db.get(Group, g_b.id)],
            admin=admin_b,
        )
        await db.commit()
        expired_a, expired_b = u_a.username, u_b.username

        print("--- Expired users (tenant scope) ---")
        list_a = await user_op.get_expired_users(db, ExpiredUsersQuery(target="expired"), details_a)
        assert expired_a in list_a
        assert expired_b not in list_a
        list_b = await user_op.get_expired_users(db, ExpiredUsersQuery(target="expired"), details_b)
        assert expired_b in list_b
        assert expired_a not in list_b

        dry_b_before = await user_op.delete_expired_users(
            db, details_a, ExpiredUsersQuery(target="expired", dry_run=True)
        )
        assert expired_b not in dry_b_before.users
        assert expired_a in dry_b_before.users

        # dry_run delete from A must not remove B user
        await user_op.delete_expired_users(db, details_a, ExpiredUsersQuery(target="expired", dry_run=False))
        still_b = await db.get(User, u_b.id)
        assert still_b is not None, "Tenant B expired user must survive Tenant A delete"

        list_owner = await user_op.get_expired_users(db, ExpiredUsersQuery(target="expired"), owner)
        assert expired_b in list_owner

        print("  [x] Expired-user list/delete tenant isolation and owner global scope.")

        print("--- Nested host (tenant scope) ---")
        host_kwargs = {"priority": 0, "inbound_tag": inbound_tag, "port": 443}
        with patch.object(host_op, "check_host_inbound_tags", new=AsyncMock()), patch.object(
            host_op, "validate_subscription_templates", new=AsyncMock()
        ), patch("app.operation.host.host_manager.add_host", new=AsyncMock()):
            h_a = await host_op.create_host(
                db, CreateHost(remark="nested-a", address=["1.1.1.1"], **host_kwargs), details_a
            )
            h_b = await host_op.create_host(
                db, CreateHost(remark="nested-b", address=["2.2.2.2"], **host_kwargs), details_b
            )
            host_a_id, host_b_id = h_a.id, h_b.id

            own_ref = CreateHost(
                remark="with-nested",
                address=["3.3.3.3"],
                transport_settings=TransportSettings(
                    xhttp_settings=XHttpSettings(download_settings=host_a_id),
                ),
                **host_kwargs,
            )
            await host_op.validate_ds_host(db, own_ref, admin=details_a)

            cross_ref = CreateHost(
                remark="bad-nested",
                address=["4.4.4.4"],
                transport_settings=TransportSettings(
                    xhttp_settings=XHttpSettings(download_settings=host_b_id),
                ),
                **host_kwargs,
            )
            try:
                await host_op.validate_ds_host(db, cross_ref, admin=details_a)
                raise AssertionError("Tenant A must not reference Tenant B nested host")
            except HTTPException as exc:
                assert exc.status_code == 404

            await host_op.validate_ds_host(db, cross_ref, admin=owner)

        print("  [x] Nested download host tenant isolation and owner global access.")

    print("\n--- Cleanup ---")
    async with GetDB() as db:
        user_ids = []
        for uname in (expired_a, expired_b):
            if uname:
                row = (await db.execute(select(User.id).where(User.username == uname))).scalar_one_or_none()
                if row:
                    user_ids.append(row)
        if user_ids:
            await db.execute(
                delete(users_groups_association).where(users_groups_association.c.user_id.in_(user_ids))
            )
            await db.execute(delete(User).where(User.id.in_(user_ids)))
        if host_a_id or host_b_id:
            await db.execute(delete(ProxyHost).where(ProxyHost.id.in_([host_a_id, host_b_id])))
        if group_a_id:
            gids = [group_a_id]
            if admin_b_id:
                extra = (
                    await db.execute(
                        select(Group.id).where(Group.tenant_id == tenant_b_id).order_by(Group.id.desc()).limit(1)
                    )
                ).scalar_one_or_none()
                if extra:
                    gids.append(extra)
            await db.execute(delete(inbounds_groups_association).where(inbounds_groups_association.c.group_id.in_(gids)))
            await db.execute(delete(Group).where(Group.id.in_(gids)))
        for aid in (admin_a_id, admin_b_id):
            if aid:
                await db.execute(delete(Admin).where(Admin.id == aid))
        if tenant_a_id or tenant_b_id:
            await db.execute(delete(Tenant).where(Tenant.id.in_([tenant_a_id, tenant_b_id])))
        await db.commit()

    print("\nALL TASK 2 SECURITY FIX REGRESSION TESTS PASSED.")


if __name__ == "__main__":
    asyncio.run(run_tests())
