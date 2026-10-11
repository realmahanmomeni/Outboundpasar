"""Group PUT inbound_tags: omitted preserves, explicit list replaces, [] clears."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db import GetDB
from app.db.crud.group import create_group as crud_create_group, get_group_by_id, modify_group as crud_modify_group
from app.db.models import Admin, AdminRole, Group, Tenant, TenantStatus
from app.models.group import GroupCreate, GroupModify


async def _tenant_admin(db):
    tenant = Tenant(name=f"gm_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    role = (await db.execute(select(AdminRole).limit(1))).scalar_one()
    admin = Admin(username=f"adm_{uuid.uuid4().hex[:6]}", hashed_password="x", role_id=role.id, tenant_id=tenant.id)
    db.add(admin)
    await db.flush()
    return tenant, admin


async def test_modify_omitted_inbound_tags_preserves_associations():
    async with GetDB() as db:
        tenant, _admin = await _tenant_admin(db)
        tags = ["tag-a", "tag-b"]
        group = await crud_create_group(
            db,
            GroupCreate(name=f"g_{uuid.uuid4().hex[:6]}", inbound_tags=tags),
            tenant_id=tenant.id,
        )
        await crud_modify_group(
            db,
            group,
            GroupModify(name=group.name, is_disabled=True),
        )
        refreshed = await get_group_by_id(db, group.id, tenant_id=tenant.id)
        assert refreshed is not None
        assert sorted(refreshed.inbound_tags) == sorted(tags)
        await db.rollback()


async def test_modify_explicit_inbound_tags_replaces():
    async with GetDB() as db:
        tenant, _admin = await _tenant_admin(db)
        group = await crud_create_group(
            db,
            GroupCreate(name=f"g_{uuid.uuid4().hex[:6]}", inbound_tags=["tag-a", "tag-b"]),
            tenant_id=tenant.id,
        )
        await crud_modify_group(
            db,
            group,
            GroupModify(name=group.name, inbound_tags=["tag-c"]),
        )
        refreshed = await get_group_by_id(db, group.id, tenant_id=tenant.id)
        assert refreshed is not None
        assert refreshed.inbound_tags == ["tag-c"]
        await db.rollback()


async def test_modify_explicit_empty_list_clears():
    async with GetDB() as db:
        tenant, _admin = await _tenant_admin(db)
        group = await crud_create_group(
            db,
            GroupCreate(name=f"g_{uuid.uuid4().hex[:6]}", inbound_tags=["tag-a"]),
            tenant_id=tenant.id,
        )
        await crud_modify_group(
            db,
            group,
            GroupModify(name=group.name, inbound_tags=[]),
        )
        refreshed = await get_group_by_id(db, group.id, tenant_id=tenant.id)
        assert refreshed is not None
        assert refreshed.inbound_tags == []
        await db.rollback()


async def test_group_modify_model_omitted_is_none_not_empty():
    m = GroupModify.model_validate({"name": "abc", "is_disabled": False})
    assert m.inbound_tags is None
    m2 = GroupModify.model_validate({"name": "abc", "inbound_tags": []})
    assert m2.inbound_tags == []


async def test_user_create_does_not_clear_group_inbounds():
    from app.db.crud.user import create_user as crud_create_user
    from app.db.models import UserStatus
    from app.models.user import UserCreate
    from app.operation import OperatorType
    from app.operation.group import GroupOperation
    from app.operation.user import UserOperation

    group_op = GroupOperation(OperatorType.API)
    user_op = UserOperation(OperatorType.API)

    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()), patch.object(
        user_op, "check_inbound_tags", new=AsyncMock()
    ):
        async with GetDB() as db:
            tenant, admin = await _tenant_admin(db)
            group = await crud_create_group(
                db,
                GroupCreate(name=f"g_{uuid.uuid4().hex[:6]}", inbound_tags=["tag-keep"]),
                tenant_id=tenant.id,
            )
            user = await crud_create_user(
                db,
                UserCreate(
                    username=f"u_{uuid.uuid4().hex[:6]}",
                    status=UserStatus.active,
                    group_ids=[group.id],
                ),
                groups=[group],
                admin=admin,
            )
            assert user.id
            refreshed = await get_group_by_id(db, group.id, tenant_id=tenant.id)
            assert refreshed is not None
            assert refreshed.inbound_tags == ["tag-keep"]
            await db.rollback()
