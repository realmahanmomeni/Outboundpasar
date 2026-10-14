"""Phase 3-B.3 re-scope: Outbound Center managed subscription subdomains."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime as dt

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.custom_domain import create_custom_domain, get_custom_domain_by_id, upsert_managed_subdomain
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, SubscriptionCustomDomainState, SubscriptionDomainKind, Tenant, TenantStatus
from app.models.custom_domain import CustomDomainCreate, ManagedSubdomainAssociate
from app.operation.custom_domain import CustomDomainOperation
from app.operation.user import UserOperation
from app.db.crud.admin import build_admin_details
from app.services.custom_domain.managed_subdomain import (
    ManagedSubdomainRecord,
    ManagedSubdomainValidationError,
    normalize_managed_subdomain_input,
)
from app.services.custom_domain.oc_managed_subdomain_client import set_managed_subdomain_client
from app.services.custom_domain.subscription_hostname import subscription_url_prefix_for_workspace
from config import custom_domain_settings


class _FakeManagedClient:
    def __init__(self, record: ManagedSubdomainRecord | None):
        self.record = record
        self.last_expected_account: int | None = None

    async def fetch_owned_subdomain(self, integration, *, connection_token, label, expected_account_id):
        self.last_expected_account = expected_account_id
        if self.record is None or self.record.label != label:
            from app.services.custom_domain.managed_subdomain import ManagedSubdomainNotOwned

            raise ManagedSubdomainNotOwned("not owned")
        if self.record.oc_account_id != expected_account_id:
            from app.services.custom_domain.managed_subdomain import ManagedSubdomainNotOwned

            raise ManagedSubdomainNotOwned("account mismatch")
        return self.record


async def _tenant(db) -> int:
    t = Tenant(name=f"ms_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"ms_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


def test_normalize_rejects_apex_and_nested():
    with pytest.raises(ManagedSubdomainValidationError):
        normalize_managed_subdomain_input("outboundcenter.ir")
    with pytest.raises(ManagedSubdomainValidationError):
        normalize_managed_subdomain_input("a.b.outboundcenter.ir")


def test_normalize_accepts_label_or_fqdn():
    label, fqdn = normalize_managed_subdomain_input("alpha")
    assert label == "alpha"
    assert fqdn == "alpha.outboundcenter.ir"
    label2, fqdn2 = normalize_managed_subdomain_input("alpha.outboundcenter.ir")
    assert label2 == "alpha"
    assert fqdn2 == fqdn


@pytest.mark.asyncio
async def test_arbitrary_register_rejects_managed_zone(monkeypatch):
    monkeypatch.setattr(
        custom_domain_settings, "custom_domain_register_managed_zone_guard_enabled", True
    )
    op = CustomDomainOperation()
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        with pytest.raises(Exception):
            await op.register_domain(
                db,
                CustomDomainCreate(hostname="alpha.outboundcenter.ir"),
                details,
            )


@pytest.mark.asyncio
async def test_subscription_url_uses_active_managed_hostname(monkeypatch):
    monkeypatch.setattr(custom_domain_settings, "ingress_public_https_ready", True)
    monkeypatch.setattr(custom_domain_settings, "managed_subscription_hostname_in_urls_enabled", True)
    label = f"url{uuid.uuid4().hex[:6]}"
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
            oc_owner_account_id=99,
            active=True,
        )
        await db.commit()
        prefix = await subscription_url_prefix_for_workspace(
            db,
            admin.workspace_id,
            default_prefix="https://default.example",
        )
        assert prefix == f"https://{fqdn}"


@pytest.mark.asyncio
async def test_tenant_b_cannot_associate_hostname_reserved_by_a(monkeypatch):
    monkeypatch.setattr(custom_domain_settings, "oc_managed_subdomain_api_enabled", True)
    label = f"shared{uuid.uuid4().hex[:6]}"
    fqdn = f"{label}.outboundcenter.ir"
    record = ManagedSubdomainRecord(
        subdomain_id="oc-1",
        label=label,
        fqdn=fqdn,
        status="active",
        oc_account_id=1,
    )
    set_managed_subdomain_client(_FakeManagedClient(record))
    op = CustomDomainOperation()
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await upsert_managed_subdomain(
            db,
            workspace_id=admin_a.workspace_id,
            tenant_id=tenant_id,
            hostname_normalized=fqdn,
            label=label,
            oc_subdomain_id="oc-1",
            oc_owner_account_id=1,
            active=True,
        )
        await db.commit()
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)
        with pytest.raises(Exception) as exc:
            await op.associate_managed_subdomain(
                db,
                ManagedSubdomainAssociate(subdomain=label),
                details_b,
            )
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_associate_fails_closed_when_api_disabled(monkeypatch):
    monkeypatch.setattr(custom_domain_settings, "oc_managed_subdomain_api_enabled", False)
    op = CustomDomainOperation()
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        with pytest.raises(Exception) as exc:
            await op.associate_managed_subdomain(
                db,
                ManagedSubdomainAssociate(subdomain="alpha"),
                details,
            )
        assert exc.value.status_code == 503
