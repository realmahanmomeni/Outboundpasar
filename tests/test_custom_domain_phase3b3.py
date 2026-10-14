"""Phase 3-B.3: ingress architecture adaptation and fail-closed provider gating."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime as dt, timedelta as td
from unittest.mock import MagicMock

import pytest

from app.db import GetDB
from app.db.crud.custom_domain import create_custom_domain, get_custom_domain_by_id
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, SubscriptionCustomDomainState, Tenant, TenantStatus
from app.jobs.custom_domain_ssl import process_custom_domain_certificates
from app.operation.custom_domain import CustomDomainOperation
from app.services.custom_domain.caddy_provisioner import CaddyCertificateProvisioner
from app.services.custom_domain.ssl_lifecycle import (
    CertificateProvisionerError,
    CertificateProviderUnavailable,
    StubCertificateProvisioner,
    UnconfiguredCertificateProvisioner,
    get_certificate_provisioner,
    ingress_ready_for_hostname,
    set_certificate_provisioner,
)
from config import custom_domain_settings


@pytest.mark.asyncio
async def test_unconfigured_provider_cannot_issue_or_activate():
    set_certificate_provisioner(UnconfiguredCertificateProvisioner())
    async with GetDB() as db:
        tenant = Tenant(name=f"t_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        role = (await db.execute(__import__("sqlalchemy").select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin = Admin(username=f"a_{uuid.uuid4().hex[:8]}", hashed_password="x", role_id=role.id, tenant_id=tenant.id)
        db.add(admin)
        await db.flush()
        await create_workspace_for_administrator(db, admin)
        row, _ = await create_custom_domain(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant.id,
            hostname_normalized=f"blocked-{uuid.uuid4().hex[:6]}.example.com",
        )
        row.state = SubscriptionCustomDomainState.pending_certificate
        row.verified_at = dt.now(UTC)
        await db.commit()

    await process_custom_domain_certificates()

    async with GetDB() as db:
        refreshed = await get_custom_domain_by_id(db, row.id)
        assert refreshed.state != SubscriptionCustomDomainState.active
        assert refreshed.certificate_last_error


@pytest.mark.asyncio
async def test_provider_unavailable_status_is_not_activation_ready():
    set_certificate_provisioner(UnconfiguredCertificateProvisioner())
    status = await get_certificate_provisioner().provider_status(
        public_https_ready=custom_domain_settings.ingress_public_https_ready
    )
    assert status.can_activate_custom_domains is False
    assert status.configured is False


@pytest.mark.asyncio
async def test_caddy_absent_certificate_does_not_fake_readiness():
    domain_row = MagicMock()
    domain_row.id = 7
    domain_row.hostname_normalized = "tenant.example.com"
    domain_row.certificate_not_after = None

    class _Resp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload or {}

    class _Client:
        async def request(self, method, path, **kwargs):
            if path.endswith("/routes") and method == "GET":
                return _Resp(200, [])
            if path.endswith("/routes") and method == "POST":
                return _Resp(201)
            if path.startswith("/pki/certificates/local/"):
                return _Resp(404)
            if path.endswith("pasarguard_custom_domains") and method == "GET":
                return _Resp(404)
            if path.endswith("pasarguard_custom_domains") and method == "PUT":
                return _Resp(200)
            if path == "/config/":
                return _Resp(200, {})
            raise AssertionError((method, path))

        async def aclose(self):
            return None

    provisioner = CaddyCertificateProvisioner("http://127.0.0.1:2019", http_client=_Client())
    with pytest.raises(CertificateProviderUnavailable):
        await provisioner.issue(domain_row)
    assert await provisioner.certificate_ready(domain_row) is False


@pytest.mark.asyncio
async def test_disabled_domain_not_reactivated_by_stale_job():
    set_certificate_provisioner(StubCertificateProvisioner())
    async with GetDB() as db:
        tenant = Tenant(name=f"t_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        role = (await db.execute(__import__("sqlalchemy").select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin = Admin(username=f"a_{uuid.uuid4().hex[:8]}", hashed_password="x", role_id=role.id, tenant_id=tenant.id)
        db.add(admin)
        await db.flush()
        await create_workspace_for_administrator(db, admin)
        row, _ = await create_custom_domain(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant.id,
            hostname_normalized=f"disabled-{uuid.uuid4().hex[:6]}.example.com",
        )
        row.state = SubscriptionCustomDomainState.disabled
        row.verified_at = dt.now(UTC)
        await db.commit()

    await process_custom_domain_certificates()

    async with GetDB() as db:
        refreshed = await get_custom_domain_by_id(db, row.id)
        assert refreshed.state == SubscriptionCustomDomainState.disabled


@pytest.mark.asyncio
async def test_ingress_public_https_gate_blocks_activation_without_flag(monkeypatch):
    monkeypatch.setattr(custom_domain_settings, "ingress_public_https_ready", False)
    set_certificate_provisioner(StubCertificateProvisioner())
    async with GetDB() as db:
        tenant = Tenant(name=f"t_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        role = (await db.execute(__import__("sqlalchemy").select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin = Admin(username=f"a_{uuid.uuid4().hex[:8]}", hashed_password="x", role_id=role.id, tenant_id=tenant.id)
        db.add(admin)
        await db.flush()
        await create_workspace_for_administrator(db, admin)
        row, _ = await create_custom_domain(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant.id,
            hostname_normalized=f"nogate-{uuid.uuid4().hex[:6]}.example.com",
        )
        row.state = SubscriptionCustomDomainState.pending_certificate
        row.verified_at = dt.now(UTC)
        row.certificate_not_after = dt.now(UTC) + td(days=30)
        row.certificate_ref = "stub-precert"
        await db.commit()

    ready = await ingress_ready_for_hostname(
        row,
        public_https_ready=custom_domain_settings.ingress_public_https_ready,
    )
    assert ready is False


@pytest.mark.asyncio
async def test_ingress_status_operation_reports_architecture_options():
    set_certificate_provisioner(UnconfiguredCertificateProvisioner())
    op = CustomDomainOperation()
    status = await op.ingress_status()
    assert status.provider == "none"
    assert not status.can_activate_custom_domains
    assert len(status.architecture_options) >= 4


@pytest.mark.asyncio
async def test_request_certificate_rejected_when_ingress_unconfigured():
    set_certificate_provisioner(UnconfiguredCertificateProvisioner())
    op = CustomDomainOperation()
    async with GetDB() as db:
        tenant = Tenant(name=f"t_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        role = (await db.execute(__import__("sqlalchemy").select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin = Admin(username=f"a_{uuid.uuid4().hex[:8]}", hashed_password="x", role_id=role.id, tenant_id=tenant.id)
        db.add(admin)
        await db.flush()
        await create_workspace_for_administrator(db, admin)
        from app.db.crud.admin import build_admin_details

        details = build_admin_details(admin, include_loaded_metrics=False)
        row, _ = await create_custom_domain(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant.id,
            hostname_normalized=f"req-{uuid.uuid4().hex[:6]}.example.com",
        )
        row.state = SubscriptionCustomDomainState.verified
        row.verified_at = dt.now(UTC)
        await db.commit()
        with pytest.raises(Exception) as exc:
            await op.request_certificate(db, row.id, details)
        assert getattr(exc.value, "status_code", None) == 503
