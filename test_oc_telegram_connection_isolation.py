"""Per-actor Telegram OC connection isolation (owner vs tenant administrators)."""
from __future__ import annotations

import asyncio
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.db.models_oc import TenantTelegramConnection, TenantTelegramConnectionStatus
from app.db.crud.admin import build_admin_details
from app.models.admin import AdminDetails, AdminRoleData
from app.services.oc_actor_connection import (
    binding_admin_id_for_actor,
    require_oc_telegram_connection_for_actor,
    resolve_oc_telegram_connection_for_actor,
)
from app.services.oc_telegram_connection import get_active_connection


async def _tenant(db) -> int:
    t = Tenant(name=f"tg_iso_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _admin(db, tenant_id: int, name: str) -> AdminDetails:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=name, hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    return build_admin_details(row, include_loaded_metrics=False)


async def test_owner_and_tenant_admin_use_distinct_connections():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        owner = AdminDetails(
            id=1,
            username="owner",
            tenant_id=tenant_id,
            role=AdminRoleData(is_owner=True, name="owner"),
        )
        admin_a = await _admin(db, tenant_id, f"adm_a_{uuid.uuid4().hex[:4]}")
        admin_b = await _admin(db, tenant_id, f"adm_b_{uuid.uuid4().hex[:4]}")

        owner_conn = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=None,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=100,
            oc_connection_token_encrypted="enc-owner",
        )
        conn_a = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin_a.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=200,
            oc_connection_token_encrypted="enc-a",
        )
        conn_b = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin_b.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=300,
            oc_connection_token_encrypted="enc-b",
        )
        db.add_all([owner_conn, conn_a, conn_b])
        await db.flush()

        resolved_owner = await resolve_oc_telegram_connection_for_actor(db, owner, True, tenant_id)
        resolved_a = await resolve_oc_telegram_connection_for_actor(db, admin_a, False, tenant_id)
        resolved_b = await resolve_oc_telegram_connection_for_actor(db, admin_b, False, tenant_id)

        assert resolved_owner is not None and resolved_owner.id == owner_conn.id
        assert resolved_a is not None and resolved_a.id == conn_a.id
        assert resolved_b is not None and resolved_b.id == conn_b.id
        assert resolved_a.oc_account_id != resolved_owner.oc_account_id

        await db.rollback()


async def test_tenant_admin_without_connection_fails():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _admin(db, tenant_id, f"adm_none_{uuid.uuid4().hex[:4]}")
        assert binding_admin_id_for_actor(False, admin) == admin.id
        assert await get_active_connection(db, tenant_id, binding_admin_id=admin.id) is None
        try:
            await require_oc_telegram_connection_for_actor(db, admin, False, tenant_id)
            raise AssertionError("expected 403")
        except HTTPException as exc:
            assert exc.status_code == 403
            assert "Telegram connection required" in exc.detail

        await db.rollback()


async def test_tenant_admin_cannot_use_owner_binding():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _admin(db, tenant_id, f"adm_x_{uuid.uuid4().hex[:4]}")
        db.add(
            TenantTelegramConnection(
                tenant_id=tenant_id,
                binding_admin_id=None,
                status=TenantTelegramConnectionStatus.active.value,
                active=True,
                oc_account_id=1,
            )
        )
        await db.flush()
        assert await resolve_oc_telegram_connection_for_actor(db, admin, False, tenant_id) is None
        await db.rollback()


async def main():
    await test_owner_and_tenant_admin_use_distinct_connections()
    await test_tenant_admin_without_connection_fails()
    await test_tenant_admin_cannot_use_owner_binding()
    print("test_oc_telegram_connection_isolation: OK")


if __name__ == "__main__":
    asyncio.run(main())
