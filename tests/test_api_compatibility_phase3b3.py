"""Regression: existing PasarGuard API paths match 11456d3 when 3-B.3 flags are default-off."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.custom_domain import upsert_managed_subdomain
from app.db.crud.user import get_user_by_id
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User
from app.db.models_oc import TenantTelegramConnection, TenantTelegramConnectionStatus
from app.models.custom_domain import CustomDomainCreate
from app.models.user import UserNotificationResponse
from app.operation.custom_domain import CustomDomainOperation
from app.operation.user import UserOperation
from app.services.oc_telegram_connection import activate_connection
from app.db.crud.admin import build_admin_details
from app.utils.crypto import encrypt_secret
from config import custom_domain_settings


async def _tenant(db) -> int:
    t = Tenant(name=f"api_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"api_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_subscription_url_unchanged_when_managed_hostname_flags_disabled(monkeypatch):
    monkeypatch.setattr(custom_domain_settings, "managed_subscription_hostname_in_urls_enabled", False)
    monkeypatch.setattr(custom_domain_settings, "ingress_public_https_ready", True)
    label = f"api{uuid.uuid4().hex[:6]}"
    fqdn = f"{label}.outboundcenter.ir"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await upsert_managed_subdomain(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant_id,
            hostname_normalized=fqdn,
            label=label,
            oc_subdomain_id="oc-1",
            oc_owner_account_id=1,
            active=True,
        )
        user = User(username=f"u_{uuid.uuid4().hex[:8]}", admin_id=admin.id)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        db_user = await get_user_by_id(db, user.id, load_admin=True)
        assert db_user is not None
        notification = UserNotificationResponse.model_validate(db_user, from_attributes=True)
        url = await UserOperation.generate_subscription_url(notification, db=db)
        assert fqdn not in url


@pytest.mark.asyncio
async def test_telegram_activate_connection_does_not_persist_token_digest():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        pending = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin.id,
            status=TenantTelegramConnectionStatus.pending.value,
            active=False,
        )
        db.add(pending)
        await db.commit()
        token = f"plain-{uuid.uuid4().hex}"
        row = await activate_connection(
            db,
            tenant_id,
            telegram_user_id=int(uuid.uuid4().int % 9_000_000_000) + 1_000_000_000,
            oc_account_id=99,
            connection_token_encrypted=await encrypt_secret(token),
            binding_admin_id=admin.id,
        )
        await db.commit()
        await db.refresh(row)
        assert row.oc_connection_token_sha256 is None


@pytest.mark.asyncio
async def test_register_managed_zone_allowed_when_guard_disabled(monkeypatch):
    monkeypatch.setattr(custom_domain_settings, "custom_domain_register_managed_zone_guard_enabled", False)
    op = CustomDomainOperation()
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        hostname = f"guard-off-{uuid.uuid4().hex[:8]}.outboundcenter.ir"
        result = await op.register_domain(
            db,
            CustomDomainCreate(hostname=hostname),
            details,
        )
        assert result.hostname == hostname


@pytest.mark.asyncio
async def test_managed_subdomain_binding_resolves_via_encrypted_token_without_prior_digest():
    from app.services.oc_connection_token_lookup import resolve_active_connection_by_token

    token = f"bind-{uuid.uuid4().hex}"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        conn = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=admin.id,
            status=TenantTelegramConnectionStatus.active.value,
            active=True,
            oc_account_id=42,
            oc_connection_token_encrypted=await encrypt_secret(token),
        )
        db.add(conn)
        await db.commit()
        resolved = await resolve_active_connection_by_token(db, token)
        assert resolved.id == conn.id
        await db.refresh(conn)
        assert conn.oc_connection_token_sha256 is not None
