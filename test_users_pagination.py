"""Owner users list pagination: page size, totals, and non-overlapping pages."""
from __future__ import annotations

import math
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.user import create_user
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.models.user import UserCreate, UserListQuery
from app.operation import OperatorType
from app.operation.user import UserOperation
from test_workspace_isolation_util import unique_username

# read=ALL (route contract), read_simple=OWN (must not narrow full list)
_USERS_READ_ALL_READ_SIMPLE_OWN = {
    "users": {
        "read": {"scope": 2},
        "read_simple": {"scope": 1},
        "update": {"scope": 1},
        "delete": {"scope": 1},
    },
    "system": {"read": True},
}


async def _owner_details(db):
    owner = (
        await db.execute(
            select(Admin)
            .options(selectinload(Admin.role))
            .join(AdminRole)
            .where(AdminRole.is_owner.is_(True))
            .limit(1)
        )
    ).scalar_one()
    details = build_admin_details(owner, include_loaded_metrics=False)
    assert details.is_owner is True
    return details


@pytest.mark.asyncio
async def test_owner_users_pagination_limit_50_matches_reported_page_count():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        details = await _owner_details(db)
        limit = 50
        page1 = await user_op.get_users(db, details, UserListQuery(limit=limit, offset=0, sort=["-created_at"], load_sub=False))
        page2 = await user_op.get_users(db, details, UserListQuery(limit=limit, offset=limit, sort=["-created_at"], load_sub=False))

        assert page1.total == page2.total
        assert page1.total > limit
        expected_pages = math.ceil(page1.total / limit)
        assert expected_pages >= 2
        assert len(page1.users) == limit
        assert len(page2.users) == limit
        page_last = await user_op.get_users(
            db,
            details,
            UserListQuery(limit=limit, offset=(expected_pages - 1) * limit, sort=["-created_at"], load_sub=False),
        )
        assert len(page_last.users) == page1.total - (expected_pages - 1) * limit
        assert {u.id for u in page1.users}.isdisjoint({u.id for u in page2.users})

        await db.rollback()


@pytest.mark.asyncio
async def test_owner_users_pagination_limit_10_page_two_distinct():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        details = await _owner_details(db)
        limit = 10
        page1 = await user_op.get_users(db, details, UserListQuery(limit=limit, offset=0, load_sub=False))
        page2 = await user_op.get_users(db, details, UserListQuery(limit=limit, offset=limit, load_sub=False))

        assert len(page1.users) == limit
        assert len(page2.users) == limit
        assert page1.total == page2.total
        assert {u.id for u in page1.users}.isdisjoint({u.id for u in page2.users})

        await db.rollback()


@pytest.mark.asyncio
async def test_get_users_uses_read_scope_not_read_simple_for_list_filter():
    """When read=ALL and read_simple=OWN, list includes same-workspace users not owned by actor."""
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        admin_role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        custom_role = AdminRole(
            name=f"pag_scope_{uuid.uuid4().hex[:8]}",
            is_owner=False,
            permissions=_USERS_READ_ALL_READ_SIMPLE_OWN,
            limits={},
            features={},
            access={},
            hwid={},
        )
        db.add(custom_role)
        await db.flush()

        tenant = Tenant(name=f"pag_t_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()

        host_admin = Admin(
            username=unique_username("host"),
            hashed_password="x",
            role_id=admin_role.id,
            tenant_id=tenant.id,
        )
        peer_admin = Admin(
            username=unique_username("peer"),
            hashed_password="x",
            role_id=admin_role.id,
            tenant_id=tenant.id,
        )
        db.add_all([host_admin, peer_admin])
        await db.flush()
        await create_workspace_for_administrator(db, host_admin)
        await create_workspace_for_administrator(db, peer_admin)
        await db.refresh(host_admin)
        await db.refresh(peer_admin)

        actor = Admin(
            username=unique_username("actor"),
            hashed_password="x",
            role_id=custom_role.id,
            tenant_id=tenant.id,
            workspace_id=host_admin.workspace_id,
        )
        db.add(actor)
        await db.flush()

        host_user_name = f"host_u_{uuid.uuid4().hex[:6]}"
        actor_user_name = f"actor_u_{uuid.uuid4().hex[:6]}"
        peer_user_name = f"peer_u_{uuid.uuid4().hex[:6]}"
        await create_user(db, UserCreate(username=host_user_name, proxy_settings={}), groups=[], admin=host_admin)
        await create_user(db, UserCreate(username=actor_user_name, proxy_settings={}), groups=[], admin=actor)
        await create_user(db, UserCreate(username=peer_user_name, proxy_settings={}), groups=[], admin=peer_admin)

        actor_details = build_admin_details(actor, include_loaded_metrics=False)
        resp = await user_op.get_users(db, actor_details, UserListQuery(load_sub=False))

        names = {u.username for u in resp.users}
        assert host_user_name in names
        assert actor_user_name in names
        assert peer_user_name not in names
        assert resp.total >= 2

        await db.rollback()
