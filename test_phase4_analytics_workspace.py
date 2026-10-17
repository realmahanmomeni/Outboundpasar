"""Phase 4: analytics and usage workspace isolation within a tenant."""
from __future__ import annotations

import asyncio
import uuid
from test_workspace_isolation_util import unique_username, unique_tag
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, load_admin_attrs
from app.db.crud.user import create_user
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import (
    Admin,
    AdminRole,
    Node,
    NodeUserUsage,
    Tenant,
    TenantStatus,
    User,
    UserStatus,
    UserSubscriptionUpdate,
)
from app.models.admin import AdminListQuery
from app.models.stats import Period, UserCountMetric
from app.models.user import UserCreate, UsersUsageQuery
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.operation.node import NodeOperation
from app.operation.system import SystemOperation
from app.operation.user import UserOperation
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID


async def _tenant(db) -> int:
    t = Tenant(name=f"p4_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int, name: str | None = None) -> Admin:
    username = name or unique_username("adm")
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=username, hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


async def _operator(db, tenant_id: int, creator: Admin, name: str) -> Admin:
    op_role = (await db.execute(select(AdminRole).where(AdminRole.id == BUILTIN_OPERATOR_ROLE_ID))).scalar_one()
    row = Admin(username=name, hashed_password="x", role_id=op_role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    creator_details = build_admin_details(creator, include_loaded_metrics=False)
    await assign_workspace_for_new_admin(db, row, creator=creator_details)
    await db.refresh(row)
    return row


async def _user(db, admin: Admin, name: str, *, used_traffic: int = 0) -> User:
    user = await create_user(db, UserCreate(username=name, proxy_settings={}), groups=[], admin=admin)
    if used_traffic:
        user.used_traffic = used_traffic
        await db.flush()
    return user


async def _node(db) -> Node:
    node = Node(
        name=f"n_{uuid.uuid4().hex[:8]}",
        address="127.0.0.1",
        port=10000,
        api_port=62051,
        server_ca="ca",
        api_key=None,
        core_config_id=None,
    )
    db.add(node)
    await db.flush()
    return node


async def test_system_user_counts_scoped_to_workspace():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await _user(db, admin_a, f"ua1_{uuid.uuid4().hex[:4]}")
        await _user(db, admin_a, f"ua2_{uuid.uuid4().hex[:4]}")
        await _user(db, admin_b, f"ub1_{uuid.uuid4().hex[:4]}")
        await _user(db, admin_b, f"ub2_{uuid.uuid4().hex[:4]}")
        await _user(db, admin_b, f"ub3_{uuid.uuid4().hex[:4]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        stats = await SystemOperation.get_system_users_stats(db, details_a)
        assert stats.total_user == 2, stats.total_user
        await db.rollback()


async def test_system_bandwidth_not_global():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:4]}", used_traffic=100)
        await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:4]}", used_traffic=900)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        stats = await SystemOperation.get_system_users_stats(db, details_a)
        assert stats.outgoing_bandwidth == 100, stats.outgoing_bandwidth
        await db.rollback()


async def test_users_usage_aggregate_workspace_isolated():
    user_op = UserOperation(OperatorType.API)
    now = datetime.now(UTC)
    start = now - timedelta(hours=2)
    end = now + timedelta(hours=1)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_a = await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:4]}")
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:4]}")
        node = await _node(db)
        bucket = now.replace(minute=0, second=0, microsecond=0)
        db.add(
            NodeUserUsage(
                created_at=bucket,
                user_id=user_a.id,
                node_id=node.id,
                used_traffic=50,
            )
        )
        db.add(
            NodeUserUsage(
                created_at=bucket,
                user_id=user_b.id,
                node_id=node.id,
                used_traffic=500,
            )
        )
        await db.flush()
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        query = UsersUsageQuery(start=start, end=end, period=Period.hour)
        result = await user_op.get_users_usage(db, details_a, query)
        total = sum(stat.total_traffic for rows in result.stats.values() for stat in rows)
        assert total == 50, total
        await db.rollback()


async def test_subscription_agent_counts_respect_workspace_filter():
    from sqlalchemy import and_, func, select

    from app.db.crud.user import _subscription_update_from_clause

    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_a = await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:4]}")
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:4]}")
        db.add(UserSubscriptionUpdate(user_id=user_a.id, user_agent="AgentA"))
        db.add(UserSubscriptionUpdate(user_id=user_b.id, user_agent="AgentB"))
        db.add(UserSubscriptionUpdate(user_id=user_b.id, user_agent="AgentB"))
        await db.flush()

        from_clause, conditions = _subscription_update_from_clause(workspace_id=admin_a.workspace_id)
        count_a = (
            await db.execute(select(func.count()).select_from(from_clause).where(and_(*conditions)))
        ).scalar_one()
        from_clause, conditions = _subscription_update_from_clause(workspace_id=admin_b.workspace_id)
        count_b = (
            await db.execute(select(func.count()).select_from(from_clause).where(and_(*conditions)))
        ).scalar_one()
        assert count_a == 1
        assert count_b == 2
        await db.rollback()


async def test_node_user_count_metric_workspace_isolated():
    node_op = NodeOperation(OperatorType.API)
    now = datetime.now(UTC)
    start = now - timedelta(hours=2)
    end = now + timedelta(hours=1)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_a = await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:4]}")
        await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:4]}")
        node = await _node(db)
        bucket = now.replace(minute=0, second=0, microsecond=0)
        db.add(
            NodeUserUsage(
                created_at=bucket,
                user_id=user_a.id,
                node_id=node.id,
                used_traffic=10,
            )
        )
        await db.flush()
        from app.models.node import NodeUsageQuery

        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        query = NodeUsageQuery(start=start, end=end, period=Period.hour, node_id=node.id)
        metric = await node_op.get_user_count_metric(db, UserCountMetric.online, query, details_a)
        assert metric.count_during_period == 1
        await db.rollback()


async def test_admins_list_analytics_workspace_isolated():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:4]}")
        await _user(db, admin_b, f"ub1_{uuid.uuid4().hex[:4]}")
        await _user(db, admin_b, f"ub2_{uuid.uuid4().hex[:4]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        resp = await admin_op.get_admins(db, AdminListQuery(), details_a)
        assert resp.total == 1
        assert len(resp.admins) == 1
        assert resp.admins[0].username == admin_a.username
        assert resp.admins[0].total_users == 1
        await db.rollback()


async def test_system_stats_foreign_admin_username_denied():
    from app.operation.permissions import PermissionDenied, enforce_permission

    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await load_admin_attrs(admin_a, load_users=False, load_usage_logs=False, load_role=True)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            enforce_permission(details_a, "admins", "read")
        except PermissionDenied:
            return
        try:
            await SystemOperation.get_system_users_stats(db, details_a, admin_username=admin_b.username)
            raise AssertionError("cross-workspace admin_username must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_operator_inherits_workspace_analytics():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
        await _user(db, op_a, f"ua_{uuid.uuid4().hex[:4]}")
        await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:4]}")
        op_details = build_admin_details(op_a, include_loaded_metrics=False)
        stats = await SystemOperation.get_system_users_stats(db, op_details)
        assert stats.total_user == 1
        await db.rollback()


async def run_all():
    await test_system_user_counts_scoped_to_workspace()
    await test_system_bandwidth_not_global()
    await test_users_usage_aggregate_workspace_isolated()
    await test_subscription_agent_counts_respect_workspace_filter()
    await test_node_user_count_metric_workspace_isolated()
    await test_admins_list_analytics_workspace_isolated()
    await test_system_stats_foreign_admin_username_denied()
    await test_operator_inherits_workspace_analytics()
    print("test_phase4_analytics_workspace: OK")


if __name__ == "__main__":
    asyncio.run(run_all())
