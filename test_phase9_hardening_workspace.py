"""Phase 9 hardening: extended attack matrix, worker stale IDs, cross-tenant, enumeration."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, load_admin_attrs
from app.db.crud.user import create_user
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.db.models_oc import OCIntegration, OCPanel, OCUserMapping, OCSyncState
from app.models.admin import AdminSimpleListQuery
from app.models.client_template import ClientTemplateCreate, ClientTemplateType
from app.models.settings import ConfigFormat
from app.models.user import UserCreate, UserSimpleListQuery
from app.models.user_template import UserTemplateCreate
from app.operation import OperatorType
from app.operation.admin import AdminOperation
from app.operation.client_template import ClientTemplateOperation
from app.operation.permissions import PermissionDenied, enforce_permission
from app.operation.subscription import SubscriptionOperation
from app.operation.user import UserOperation
from app.operation.user_template import UserTemplateOperation
from app.services.oc_actor_connection import resolve_oc_telegram_connection_for_actor
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID
from app.services.workspace_scope import ensure_actor_panel_access
from app.db.models_oc import TenantTelegramConnection, TenantTelegramConnectionStatus
from test_workspace_isolation_util import unique_username


async def _tenant(db, label: str = "p9h") -> int:
    t = Tenant(name=f"{label}_{uuid.uuid4().hex[:10]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=unique_username("adm"), hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


async def _operator(db, tenant_id: int, creator: Admin) -> Admin:
    op_role = (await db.execute(select(AdminRole).where(AdminRole.id == BUILTIN_OPERATOR_ROLE_ID))).scalar_one()
    row = Admin(username=unique_username("op"), hashed_password="x", role_id=op_role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    creator_details = build_admin_details(creator, include_loaded_metrics=False)
    await assign_workspace_for_new_admin(db, row, creator=creator_details)
    await db.refresh(row)
    await load_admin_attrs(row, load_role=True, load_users=False, load_usage_logs=False)
    return row


async def _integration(db) -> OCIntegration:
    integ = OCIntegration(
        base_url="https://oc.example",
        api_token_encrypted="enc",
        token_preview="prev",
        is_active=True,
    )
    db.add(integ)
    await db.flush()
    return integ


async def _panel(db, integ: OCIntegration, tenant_id: int, workspace_id: int) -> OCPanel:
    p = OCPanel(
        integration_id=integ.id,
        source_panel_id=str(uuid.uuid4().int)[:10],
        purchaser_identity="1",
        name="panel",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        oc_account_id=1,
        sync_status="connected",
    )
    db.add(p)
    await db.flush()
    return p


async def test_cross_tenant_admin_lookup_denied():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_a = await _tenant(db, "ta")
        tenant_b = await _tenant(db, "tb")
        admin_a = await _administrator(db, tenant_a)
        admin_b = await _administrator(db, tenant_b)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await admin_op.get_validated_admin_by_id(db, admin_b.id, current_admin=details_a)
            raise AssertionError("cross-tenant admin fetch must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_panel_cross_workspace_denied():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = await _integration(db)
        panel_b = await _panel(db, integ, tenant_id, admin_b.workspace_id)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await ensure_actor_panel_access(db, panel_b, is_owner=False, admin=details_a)
            raise AssertionError("panel B must not be accessible to admin A")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_subscription_cross_workspace_denied():
    sub_op = SubscriptionOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_b = await create_user(
            db,
            UserCreate(username=unique_username("ub"), proxy_settings={}),
            groups=[],
            admin=admin_b,
        )
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await sub_op.user_subscription_by_id(
                db, user_b.id, details_a, ConfigFormat.links, request_url="https://example/sub"
            )
            raise AssertionError("subscription for user B must be denied to admin A")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_client_template_cross_workspace_denied():
    tpl_op = ClientTemplateOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)
        tpl_content = '{"inbounds":[{"tag":"t"}],"outbounds":[{"tag":"o"}]}'
        created = await tpl_op.create_client_template(
            db,
            ClientTemplateCreate(
                name=unique_username("ctpl"),
                template_type=ClientTemplateType.xray_subscription,
                content=tpl_content,
            ),
            details_b,
        )
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await tpl_op.get_validated_client_template(db, created.id, admin=details_a)
            raise AssertionError("workspace B client template must not resolve for admin A")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_user_template_cross_workspace_denied():
    ut_op = UserTemplateOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        from app.db.crud.group import create_group as crud_create_group
        from app.models.group import GroupCreate

        details_b = build_admin_details(admin_b, include_loaded_metrics=False)
        group_b = await crud_create_group(
            db,
            GroupCreate(name=unique_username("gb"), inbound_tags=["VLESS TCP REALITY"]),
            tenant_id=tenant_id,
            workspace_id=admin_b.workspace_id,
        )
        tpl_b = await ut_op.create_user_template(
            db,
            UserTemplateCreate(name=unique_username("utpl"), group_ids=[group_b.id]),
            details_b,
        )
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await ut_op.get_validated_user_template(db, tpl_b.id, admin=details_a)
            raise AssertionError("user template B must not resolve for admin A")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_simple_list_does_not_enumerate_foreign_workspace_users():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a)
        foreign_name = unique_username("fu")
        await create_user(
            db,
            UserCreate(username=foreign_name, proxy_settings={}),
            groups=[],
            admin=admin_b,
        )
        op_details = build_admin_details(op_a, include_loaded_metrics=False)
        resp = await user_op.get_users_simple(
            db, op_details, UserSimpleListQuery(search=foreign_name)
        )
        assert resp.total == 0
        assert not resp.users
        await db.rollback()


async def test_admin_simple_list_does_not_enumerate_foreign_workspace_admins():
    admin_op = AdminOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        op_b = await _operator(db, tenant_id, admin_b)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        resp = await admin_op.get_admins_simple(
            db, AdminSimpleListQuery(search=op_b.username), details_a
        )
        assert resp.total == 0
        await db.rollback()


async def test_telegram_connection_resolves_per_workspace_binding():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        conn_a = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin_a.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=101,
        )
        conn_b = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin_b.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=202,
        )
        db.add_all([conn_a, conn_b])
        await db.flush()
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        resolved = await resolve_oc_telegram_connection_for_actor(db, details_a, False, tenant_id)
        assert resolved is not None and resolved.oc_account_id == 101
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)
        resolved_b = await resolve_oc_telegram_connection_for_actor(db, details_b, False, tenant_id)
        assert resolved_b is not None and resolved_b.oc_account_id == 202
        await db.rollback()


async def test_operator_nodes_permission_denied():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a)
        details = build_admin_details(op_a, include_loaded_metrics=False)
        try:
            enforce_permission(details, "nodes", "read")
            raise AssertionError("operator must not have nodes.read")
        except PermissionDenied:
            pass
        await db.rollback()


async def test_process_oc_sync_workspace_mismatch_does_not_call_oc():
    """Stale job user_id (workspace A) + panel_id (workspace B) must abort before HTTP."""
    from app.jobs import process_oc_sync as poc

    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = await _integration(db)
        panel_b = await _panel(db, integ, tenant_id, admin_b.workspace_id)
        user_a = await create_user(
            db,
            UserCreate(username=unique_username("ua"), proxy_settings={}),
            groups=[],
            admin=admin_a,
        )
        mapping = OCUserMapping(
            user_id=user_a.id,
            panel_id=panel_b.id,
            external_user_id=str(uuid.uuid4()),
            status="pending",
        )
        db.add(mapping)
        job = OCSyncState(
            entity_type="user_mapping",
            entity_id=f"{user_a.id}_{panel_b.id}",
            operation="update",
            idempotency_key=f"sync_{uuid.uuid4()}",
            payload={"groups": [], "configs": ["x"]},
            status="pending",
            revision=1,
        )
        db.add(job)
        await db.flush()
        job_id = job.id
        await db.commit()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_put = AsyncMock()
        mock_session.put = mock_put

        with patch.object(poc.aiohttp, "ClientSession", return_value=mock_session):
            with patch.object(poc, "panel_has_active_connection", AsyncMock(return_value=True)):
                await poc.process_oc_sync()

        async with GetDB() as db2:
            persisted = await db2.get(OCSyncState, job_id)
            assert persisted is not None
            assert persisted.attempts >= 1
            assert persisted.last_error and "Workspace mismatch" in persisted.last_error
            # Our job must fail before any OC user PUT for this entity.
            put_urls = [str(c) for c in mock_put.call_args_list]
            assert not any(f"{user_a.id}_{panel_b.id}" in u for u in put_urls)


async def test_user_panel_workspace_compatible_matrix():
    from types import SimpleNamespace

    from app.node.oc_sync import _user_panel_workspace_compatible

    u = SimpleNamespace(workspace_id=1)
    p_match = SimpleNamespace(workspace_id=1)
    p_other = SimpleNamespace(workspace_id=2)
    assert _user_panel_workspace_compatible(u, p_match) is True
    assert _user_panel_workspace_compatible(u, p_other) is False


async def run_all():
    await test_cross_tenant_admin_lookup_denied()
    await test_panel_cross_workspace_denied()
    await test_subscription_cross_workspace_denied()
    await test_client_template_cross_workspace_denied()
    await test_user_template_cross_workspace_denied()
    await test_simple_list_does_not_enumerate_foreign_workspace_users()
    await test_admin_simple_list_does_not_enumerate_foreign_workspace_admins()
    await test_telegram_connection_resolves_per_workspace_binding()
    await test_operator_nodes_permission_denied()
    await test_process_oc_sync_workspace_mismatch_does_not_call_oc()
    await test_user_panel_workspace_compatible_matrix()
    print("test_phase9_hardening_workspace: 11 passed")


if __name__ == "__main__":
    asyncio.run(run_all())
