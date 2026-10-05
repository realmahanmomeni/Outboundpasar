"""Phase 1: workspace ownership, panel isolation, operator inheritance, Telegram revoke scope."""
from __future__ import annotations

import asyncio
import uuid
from test_workspace_isolation_util import unique_username, unique_tag

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, Workspace
from app.db.models_oc import OCPanel, OCIntegration, TenantTelegramConnection, TenantTelegramConnectionStatus
from app.models.admin import AdminCreate, AdminDetails, AdminRoleData
from app.routers.integration import sync_configs_and_hosts
from app.routers.panel import get_panel, list_panels
from app.services.oc_actor_connection import resolve_oc_telegram_connection_for_actor
from app.services.oc_telegram_connection import revoke_connection
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID
from app.services.oc_connection_credentials import connection_token_for_panel
from app.services.workspace_scope import ensure_actor_panel_access


async def _tenant(db) -> int:
    t = Tenant(name=f"ws_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int, name: str | None = None) -> AdminDetails:
    username = name or unique_username("adm")
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=username, hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return build_admin_details(row, include_loaded_metrics=False)


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


async def _panel(db, integ: OCIntegration, tenant_id: int, workspace_id: int, oc_account: int, name: str) -> OCPanel:
    p = OCPanel(
        integration_id=integ.id,
        source_panel_id=str(uuid.uuid4().int)[:8],
        purchaser_identity=str(oc_account),
        name=name,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        oc_account_id=oc_account,
    )
    db.add(p)
    await db.flush()
    return p


async def test_same_tenant_different_workspaces():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        assert admin_a.tenant_id == admin_b.tenant_id == tenant_id
        assert admin_a.workspace_id is not None and admin_b.workspace_id is not None
        assert admin_a.workspace_id != admin_b.workspace_id
        await db.rollback()


async def test_operator_inherits_creator_workspace():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        op_role = (await db.execute(select(AdminRole).where(AdminRole.id == BUILTIN_OPERATOR_ROLE_ID))).scalar_one()
        op_row = Admin(
            username=unique_username("op"),
            hashed_password="x",
            role_id=op_role.id,
            tenant_id=tenant_id,
        )
        db.add(op_row)
        await db.flush()
        await assign_workspace_for_new_admin(db, op_row, creator=admin_a)
        assert op_row.workspace_id == admin_a.workspace_id
        await db.rollback()


async def test_panel_workspace_isolation_list_and_detail():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = await _integration(db)

        db.add(
            TenantTelegramConnection(
                tenant_id=tenant_id,
                binding_admin_id=admin_a.id,
                status=TenantTelegramConnectionStatus.active.value,
                active=True,
                oc_account_id=100,
            )
        )
        db.add(
            TenantTelegramConnection(
                tenant_id=tenant_id,
                binding_admin_id=admin_b.id,
                status=TenantTelegramConnectionStatus.active.value,
                active=True,
                oc_account_id=200,
            )
        )
        await db.flush()

        panel_a = await _panel(db, integ, tenant_id, admin_a.workspace_id, 100, "Panel A")
        panel_b = await _panel(db, integ, tenant_id, admin_b.workspace_id, 200, "Panel B")

        ctx_a = (admin_a.username, False, admin_a)
        listed = await list_panels(db, ctx_a)
        assert {p.id for p in listed} == {panel_a.id}

        try:
            await get_panel(panel_a.id, db, ctx_a)
        except HTTPException:
            raise AssertionError("should read own panel") from None

        ctx_b = (admin_b.username, False, admin_b)
        try:
            await get_panel(panel_a.id, db, ctx_b)
            raise AssertionError("Admin B must not read Panel A")
        except HTTPException as exc:
            assert exc.status_code == 404

        await db.rollback()


async def test_integration_panel_sync_idor():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = await _integration(db)
        panel_b = await _panel(db, integ, tenant_id, admin_b.workspace_id, 200, "Panel B")

        ctx_a = (admin_a.username, False, admin_a)
        from app.routers.integration import SyncRequest

        try:
            await sync_configs_and_hosts(
                panel_b.id,
                SyncRequest(selected_group_ids=[], group_names={}),
                db,
                ctx_a,
            )
            raise AssertionError("Admin A must not sync Panel B")
        except HTTPException as exc:
            assert exc.status_code == 404

        await db.rollback()


async def test_ensure_actor_panel_access_same_tenant():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = await _integration(db)
        panel_b = await _panel(db, integ, tenant_id, admin_b.workspace_id, 200, "Panel B")

        try:
            await ensure_actor_panel_access(db, panel_b, is_owner=False, admin=admin_a, identity=None)
            raise AssertionError("cross-workspace panel access must fail")
        except HTTPException as exc:
            assert exc.status_code == 404

        await db.rollback()


async def test_telegram_connections_remain_distinct():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        conn_a = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin_a.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=100,
        )
        conn_b = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin_b.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=200,
        )
        db.add_all([conn_a, conn_b])
        await db.flush()

        assert (await resolve_oc_telegram_connection_for_actor(db, admin_a, False, tenant_id)).id == conn_a.id
        assert (await resolve_oc_telegram_connection_for_actor(db, admin_b, False, tenant_id)).id == conn_b.id
        await db.rollback()


async def test_owner_lists_all_panels():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = await _integration(db)
        panel_a = await _panel(db, integ, tenant_id, admin_a.workspace_id, 100, "A")
        panel_b = await _panel(db, integ, tenant_id, admin_b.workspace_id, 200, "B")
        owner = AdminDetails(
            id=1,
            username="owner",
            tenant_id=tenant_id,
            role=AdminRoleData(is_owner=True, name="owner"),
        )
        listed = await list_panels(db, (owner.username, True, owner))
        listed_ids = {p.id for p in listed}
        assert panel_a.id in listed_ids and panel_b.id in listed_ids
        await db.rollback()


async def test_connection_token_for_panel_uses_workspace_owner_binding():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = await _integration(db)
        conn_a = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin_a.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=100,
            oc_connection_token_encrypted="enc-a",
        )
        conn_b = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin_b.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=200,
            oc_connection_token_encrypted="enc-b",
        )
        db.add_all([conn_a, conn_b])
        await db.flush()

        panel_a = await _panel(db, integ, tenant_id, admin_a.workspace_id, 100, "Panel A")
        panel_b = await _panel(db, integ, tenant_id, admin_b.workspace_id, 200, "Panel B")
        # Workspace B panel must not use Administrator A's Telegram binding even if oc_account_id matched A's.
        panel_trap = OCPanel(
            integration_id=integ.id,
            source_panel_id="trap",
            purchaser_identity="100",
            name="Trap",
            tenant_id=tenant_id,
            workspace_id=admin_b.workspace_id,
            oc_account_id=100,
        )
        db.add(panel_trap)
        await db.flush()

        import app.services.oc_connection_credentials as creds
        from app.services.oc_integration_client import OcConnectionTokenMissing

        orig = creds.decrypt_secret

        async def _fake_decrypt(enc: str) -> str:
            if enc == "enc-a":
                return "token-a"
            if enc == "enc-b":
                return "token-b"
            return "unknown"

        creds.decrypt_secret = _fake_decrypt
        try:
            assert await connection_token_for_panel(db, panel_a) == "token-a"
            assert await connection_token_for_panel(db, panel_b) == "token-b"
            try:
                await connection_token_for_panel(db, panel_trap)
                raise AssertionError("expected missing connection for B workspace + A oc_account")
            except OcConnectionTokenMissing:
                pass
        finally:
            creds.decrypt_secret = orig

        await db.rollback()


async def test_revoke_scopes_panel_deletions_by_workspace():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = await _integration(db)
        await _panel(db, integ, tenant_id, admin_a.workspace_id, 100, "A")
        await _panel(db, integ, tenant_id, admin_b.workspace_id, 200, "B")

        from app.db.models_oc import OCUserMapping
        from app.db.models import User

        user = User(username=f"u_{uuid.uuid4().hex[:6]}", admin_id=admin_a.id)
        db.add(user)
        await db.flush()
        panels = (await db.execute(select(OCPanel))).scalars().all()
        for panel in panels:
            db.add(
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel.id,
                    external_user_id=f"ext_{panel.id}",
                )
            )
        await db.flush()

        class _Track:
            workspace_calls: list[int] = []
            tenant_calls: list[int] = []

        async def _ws(db_session, workspace_id, *, connection_id=None):
            _Track.workspace_calls.append(workspace_id)

        async def _tenant_del(db_session, tenant_id, *, connection_id=None):
            _Track.tenant_calls.append(tenant_id)

        import app.node.oc_sync as oc_sync_mod

        orig_ws = oc_sync_mod.enqueue_workspace_panel_user_deletions
        orig_t = oc_sync_mod.enqueue_tenant_panel_user_deletions
        oc_sync_mod.enqueue_workspace_panel_user_deletions = _ws
        oc_sync_mod.enqueue_tenant_panel_user_deletions = _tenant_del
        try:
            db.add(
                TenantTelegramConnection(
                    tenant_id=tenant_id,
                    binding_admin_id=admin_a.id,
                    status=TenantTelegramConnectionStatus.active.value,
                    active=True,
                    oc_account_id=100,
                )
            )
            await db.flush()
            await revoke_connection(db, tenant_id, binding_admin_id=admin_a.id)
            assert _Track.workspace_calls == [admin_a.workspace_id]
            assert _Track.tenant_calls == []
        finally:
            oc_sync_mod.enqueue_workspace_panel_user_deletions = orig_ws
            oc_sync_mod.enqueue_tenant_panel_user_deletions = orig_t

        await db.rollback()


async def _main():
    await test_same_tenant_different_workspaces()
    await test_operator_inherits_creator_workspace()
    await test_panel_workspace_isolation_list_and_detail()
    await test_integration_panel_sync_idor()
    await test_ensure_actor_panel_access_same_tenant()
    await test_telegram_connections_remain_distinct()
    await test_owner_lists_all_panels()
    await test_connection_token_for_panel_uses_workspace_owner_binding()
    await test_revoke_scopes_panel_deletions_by_workspace()
    print("test_phase1_workspace: OK")


if __name__ == "__main__":
    asyncio.run(_main())
