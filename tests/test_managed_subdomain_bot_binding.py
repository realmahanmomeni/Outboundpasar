"""Bot-provisioned managed subdomain claims and Settings confirmation."""
from __future__ import annotations

import uuid

import pytest

from app.db import GetDB
from app.db.crud.managed_subdomain_binding import upsert_bot_binding
from app.db.crud.custom_domain import get_managed_domain_for_workspace
from app.db.crud.managed_subdomain_binding import get_binding_for_claim
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, SubscriptionCustomDomainState, Tenant, TenantStatus
from app.models.managed_subdomain_integration import ManagedSubdomainSettingsUpdate
from app.operation.custom_domain import CustomDomainOperation
from app.db.crud.admin import build_admin_details
from config import custom_domain_settings
from sqlalchemy import select


async def _tenant(db) -> int:
    t = Tenant(name=f"bot_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"bot_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_settings_rejects_label_without_bot_claim(monkeypatch):
    monkeypatch.setattr(custom_domain_settings, "ingress_public_https_ready", True)
    op = CustomDomainOperation()
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        with pytest.raises(Exception) as exc:
            await op.update_managed_subdomain_settings(
                db,
                ManagedSubdomainSettingsUpdate(label="alpha"),
                details,
            )
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_bot_binding_then_settings_claim_activates_subdomain(monkeypatch):
    monkeypatch.setattr(custom_domain_settings, "ingress_public_https_ready", True)
    label = f"t{uuid.uuid4().hex[:6]}"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        binding = await upsert_bot_binding(
            db,
            tenant_id=tenant_id,
            workspace_id=admin.workspace_id,
            binding_admin_id=admin.id,
            label=label,
            fqdn=f"{label}.outboundcenter.ir",
            oc_subdomain_id=f"oc-{uuid.uuid4().hex[:8]}",
            oc_account_id=1001,
            idempotency_key=uuid.uuid4().hex,
        )
        await db.commit()
        assert binding.status == "pending_claim"

    op = CustomDomainOperation()
    async with GetDB() as db:
        details = build_admin_details(admin, include_loaded_metrics=False)
        settings = await op.update_managed_subdomain_settings(
            db,
            ManagedSubdomainSettingsUpdate(label=label),
            details,
        )
        assert settings.label == label
        domain = await get_managed_domain_for_workspace(db, admin.workspace_id)
        assert domain is not None
        assert domain.state == SubscriptionCustomDomainState.active
        binding = await get_binding_for_claim(
            db,
            workspace_id=admin.workspace_id,
            binding_admin_id=admin.id,
            label=label,
        )
        assert binding is None
