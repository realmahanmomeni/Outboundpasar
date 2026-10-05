"""Phase 3: group and host workspace isolation within a tenant."""
from __future__ import annotations

import uuid
from test_workspace_isolation_util import unique_username, unique_tag
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.group import create_group as crud_create_group
from app.db.crud.host import create_host as crud_create_host
from app.db.crud.user import create_user
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Group, ProxyHost, Tenant, TenantStatus
from app.models.group import BulkGroup, GroupCreate, GroupListQuery, GroupModify, GroupSimpleListQuery
from app.models.host import CreateHost, HostListQuery
from app.models.user import UserCreate
from app.operation import OperatorType
from app.operation.group import GroupOperation
from app.operation.host import HostOperation
from app.operation.user import UserOperation
from app.operation.user_template import UserTemplateOperation
from app.models.user_template import UserTemplateCreate
from app.services.assignable_hosts import list_group_host_options
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID


async def _tenant(db) -> int:
    t = Tenant(name=f"p3_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
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


def _host_op_patches():
    stack = ExitStack()
    stack.enter_context(patch.object(HostOperation, "check_host_inbound_tags", new=AsyncMock()))
    stack.enter_context(patch.object(HostOperation, "validate_ds_host", new=AsyncMock(return_value=None)))
    stack.enter_context(patch.object(HostOperation, "validate_subscription_templates", new=AsyncMock()))
    stack.enter_context(patch("app.operation.host.host_manager.add_host", new=AsyncMock()))
    stack.enter_context(patch("app.operation.host.host_manager.add_hosts", new=AsyncMock()))
    stack.enter_context(patch("app.operation.host.host_manager.remove_host", new=AsyncMock()))
    return stack


async def test_same_tenant_admin_group_list_isolation():
    group_op = GroupOperation(OperatorType.API)
    inbound_tag = "VLESS TCP REALITY"
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            details_a = build_admin_details(admin_a, include_loaded_metrics=False)
            details_b = build_admin_details(admin_b, include_loaded_metrics=False)
            g_a = await group_op.create_group(
                db, GroupCreate(name=f"ga_{uuid.uuid4().hex[:4]}", inbound_tags=[inbound_tag]), details_a
            )
            g_b = await group_op.create_group(
                db, GroupCreate(name=f"gb_{uuid.uuid4().hex[:4]}", inbound_tags=[inbound_tag]), details_b
            )
            list_a = await group_op.get_all_groups(db, GroupListQuery(), details_a)
            ids_a = {g.id for g in list_a.groups}
            assert g_a.id in ids_a
            assert g_b.id not in ids_a
            assert list_a.total == len(list_a.groups)
            await db.rollback()


async def test_group_detail_idor_same_tenant():
    group_op = GroupOperation(OperatorType.API)
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            details_a = build_admin_details(admin_a, include_loaded_metrics=False)
            details_b = build_admin_details(admin_b, include_loaded_metrics=False)
            g_b = await group_op.create_group(
                db,
                GroupCreate(name=f"gb_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]),
                details_b,
            )
            try:
                await group_op._get_group_with_access(db, g_b.id, details_a)
                raise AssertionError("cross-workspace group detail must fail")
            except HTTPException as exc:
                assert exc.status_code == 404
            await db.rollback()


async def test_group_search_and_simple_list_isolation():
    group_op = GroupOperation(OperatorType.API)
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            details_a = build_admin_details(admin_a, include_loaded_metrics=False)
            details_b = build_admin_details(admin_b, include_loaded_metrics=False)
            name_b = f"uniq_{uuid.uuid4().hex[:6]}"
            await group_op.create_group(
                db, GroupCreate(name=name_b[:16], inbound_tags=["VLESS TCP REALITY"]), details_b
            )
            simple = await group_op.get_groups_simple(
                db, GroupSimpleListQuery(search=name_b[:8]), details_a
            )
            assert simple.total == 0
            assert not simple.groups
            simple_b = await group_op.get_groups_simple(
                db, GroupSimpleListQuery(search=name_b[:8]), details_b
            )
            assert simple_b.total >= 1
            await db.rollback()


async def test_group_user_membership_cross_workspace_denied():
    user_op = UserOperation(OperatorType.API)
    group_op = GroupOperation(OperatorType.API)
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            details_a = build_admin_details(admin_a, include_loaded_metrics=False)
            details_b = build_admin_details(admin_b, include_loaded_metrics=False)
            g_b = await group_op.create_group(
                db, GroupCreate(name=f"gb_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]), details_b
            )
            try:
                await user_op.create_user(
                    db,
                    UserCreate(username=f"u_{uuid.uuid4().hex[:6]}", group_ids=[g_b.id]),
                    details_a,
                )
                raise AssertionError("assign foreign workspace group must fail")
            except HTTPException as exc:
                assert exc.status_code == 404
            await db.rollback()


async def test_bulk_add_groups_cross_workspace_group_id():
    group_op = GroupOperation(OperatorType.API)
    user_op = UserOperation(OperatorType.API)
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            details_a = build_admin_details(admin_a, include_loaded_metrics=False)
            details_b = build_admin_details(admin_b, include_loaded_metrics=False)
            g_a = await group_op.create_group(
                db, GroupCreate(name=f"ga_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]), details_a
            )
            g_b = await group_op.create_group(
                db, GroupCreate(name=f"gb_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]), details_b
            )
            user_a = await create_user(
                db,
                UserCreate(username=f"ua_{uuid.uuid4().hex[:6]}"),
                groups=[],
                admin=admin_a,
            )
            try:
                await group_op.bulk_add_groups(
                    db,
                    BulkGroup(group_ids={g_b.id}, users={user_a.id}),
                    details_a,
                )
                raise AssertionError("bulk add foreign group must fail")
            except HTTPException as exc:
                assert exc.status_code == 404
            dry = await group_op.bulk_add_groups(
                db,
                BulkGroup(group_ids={g_a.id}, users={user_a.id}, dry_run=True),
                details_a,
            )
            assert dry.affected_users == 1
            await db.rollback()


async def test_host_list_and_idor_same_tenant():
    host_op = HostOperation(OperatorType.API)
    host_kwargs = {"priority": 0, "inbound_tag": "VLESS TCP REALITY", "port": 443}
    with _host_op_patches():
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            details_a = build_admin_details(admin_a, include_loaded_metrics=False)
            details_b = build_admin_details(admin_b, include_loaded_metrics=False)
            h_a = await host_op.create_host(
                db, CreateHost(remark="ha", address=["1.1.1.1"], **host_kwargs), details_a
            )
            h_b = await host_op.create_host(
                db, CreateHost(remark="hb", address=["2.2.2.2"], **host_kwargs), details_b
            )
            listed = await host_op.get_hosts(db, HostListQuery(), details_a)
            assert h_a.id in {x.id for x in listed}
            assert h_b.id not in {x.id for x in listed}
            try:
                await host_op.get_validated_host(db, h_b.id, admin=details_a)
                raise AssertionError("cross-workspace host must fail")
            except HTTPException as exc:
                assert exc.status_code == 404
            await db.rollback()


async def test_operator_inherits_workspace_for_groups_and_hosts():
    group_op = GroupOperation(OperatorType.API)
    host_op = HostOperation(OperatorType.API)
    host_kwargs = {"priority": 0, "inbound_tag": "VLESS TCP REALITY", "port": 443}
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()), _host_op_patches():
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
            op_details = build_admin_details(op_a, include_loaded_metrics=False)
            details_b = build_admin_details(admin_b, include_loaded_metrics=False)
            g_b = await group_op.create_group(
                db, GroupCreate(name=f"gb_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]), details_b
            )
            h_b = await host_op.create_host(
                db, CreateHost(remark="hb", address=["9.9.9.9"], **host_kwargs), details_b
            )
            try:
                await group_op._get_group_with_access(db, g_b.id, op_details)
                raise AssertionError("operator must not access other workspace group")
            except HTTPException as exc:
                assert exc.status_code == 404
            try:
                await host_op.get_validated_host(db, h_b.id, admin=op_details)
                raise AssertionError("operator must not access other workspace host")
            except HTTPException as exc:
                assert exc.status_code == 404
            await db.rollback()


async def test_native_host_picker_scoped_to_workspace():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        inbound_tag = f"native_{uuid.uuid4().hex[:6]}"
        await crud_create_host(
            db,
            CreateHost(remark="picker-a", address=["1.0.0.1"], priority=0, inbound_tag=inbound_tag, port=443),
            tenant_id=tenant_id,
            workspace_id=admin_a.workspace_id,
        )
        await crud_create_host(
            db,
            CreateHost(remark="picker-b", address=["2.0.0.2"], priority=0, inbound_tag=f"{inbound_tag}_b", port=443),
            tenant_id=tenant_id,
            workspace_id=admin_b.workspace_id,
        )
        opts_a = await list_group_host_options(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        tags_a = {o.inbound_tag for o in opts_a}
        assert inbound_tag in tags_a
        assert f"{inbound_tag}_b" not in tags_a
        await db.rollback()


async def test_legacy_unassigned_host_invisible_to_workspace_admin():
    host_op = HostOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        legacy = await crud_create_host(
            db,
            CreateHost(remark="legacy-global", address=["10.0.0.1"], priority=0, inbound_tag="VLESS TCP REALITY", port=443),
            tenant_id=None,
            workspace_id=None,
        )
        legacy_id = legacy.id
        try:
            await host_op.get_validated_host(db, legacy_id, admin=details_a)
            raise AssertionError("legacy host without workspace must not be accessible")
        except HTTPException as exc:
            assert exc.status_code == 404
        listed = await host_op.get_hosts(db, HostListQuery(), details_a)
        assert legacy_id not in {h.id for h in listed}
        await db.rollback()


async def test_user_template_cross_workspace_group_denied():
    template_op = UserTemplateOperation(OperatorType.API)
    group_op = GroupOperation(OperatorType.API)
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            details_a = build_admin_details(admin_a, include_loaded_metrics=False)
            details_b = build_admin_details(admin_b, include_loaded_metrics=False)
            g_b = await group_op.create_group(
                db,
                GroupCreate(name=f"gb_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]),
                details_b,
            )
            try:
                await template_op.create_user_template(
                    db,
                    UserTemplateCreate(name=f"tpl_{uuid.uuid4().hex[:6]}", group_ids=[g_b.id]),
                    details_a,
                )
                raise AssertionError("template with foreign workspace group must fail")
            except HTTPException as exc:
                assert exc.status_code == 404
            await db.rollback()


async def test_group_create_sets_workspace_id():
    group_op = GroupOperation(OperatorType.API)
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            details_a = build_admin_details(admin_a, include_loaded_metrics=False)
            g = await group_op.create_group(
                db, GroupCreate(name=f"g_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]), details_a
            )
            row = await db.get(Group, g.id)
            assert row.workspace_id == admin_a.workspace_id
            assert row.tenant_id == tenant_id
            await db.rollback()


async def run_all():
    tests = [
        test_same_tenant_admin_group_list_isolation,
        test_group_detail_idor_same_tenant,
        test_group_search_and_simple_list_isolation,
        test_group_user_membership_cross_workspace_denied,
        test_bulk_add_groups_cross_workspace_group_id,
        test_host_list_and_idor_same_tenant,
        test_operator_inherits_workspace_for_groups_and_hosts,
        test_native_host_picker_scoped_to_workspace,
        test_group_create_sets_workspace_id,
        test_legacy_unassigned_host_invisible_to_workspace_admin,
        test_user_template_cross_workspace_group_denied,
    ]
    for test in tests:
        await test()
        print(f"OK {test.__name__}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_all())
