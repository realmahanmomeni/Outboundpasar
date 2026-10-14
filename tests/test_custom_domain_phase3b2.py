"""Phase 3-B.2: production DNS resolver, certificate provider, routing, and TLS permission."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime as dt, timedelta as td
from unittest.mock import MagicMock, patch

import dns.resolver
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.app_factory import create_app
from app.db import GetDB
from app.db.crud.custom_domain import create_custom_domain, get_custom_domain_by_id
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, SubscriptionCustomDomainState, Tenant, TenantStatus
from app.jobs.custom_domain_ssl import process_custom_domain_certificates
from app.models.custom_domain import CustomDomainCreate
from app.operation.custom_domain import CustomDomainOperation
from app.db.crud.admin import build_admin_details
from app.services.custom_domain.caddy_provisioner import CaddyCertificateProvisioner
from app.services.custom_domain.dns import dns_verification_matches, set_dns_txt_resolver
from app.services.custom_domain.dns_errors import DnsNoData, DnsNxDomain, DnsTimeout
from app.services.custom_domain.dns_resolver import ProductionDnsTxtResolver
from app.services.custom_domain.normalization import verification_txt_record_name, verification_txt_record_value
from app.services.custom_domain.proxy_permission import hostname_may_receive_tls_certificate
from app.services.custom_domain.ssl_lifecycle import (
    CertificateProviderUnavailable,
    CertificateRateLimited,
    StubCertificateProvisioner,
    set_certificate_provisioner,
)


class _FakeResolver:
    def __init__(self, mapping: dict[str, list[str]] | None = None, *, error: Exception | None = None):
        self.mapping = mapping or {}
        self.error = error

    async def lookup_txt(self, name: str) -> list[str]:
        if self.error is not None:
            raise self.error
        return self.mapping.get(name, [])


async def _tenant(db) -> int:
    t = Tenant(name=f"cd_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"cd_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


def test_production_resolver_rejects_arbitrary_query_names():
    resolver = ProductionDnsTxtResolver()
    with pytest.raises(Exception):
        resolver._lookup_sync("evil.internal")


def test_production_resolver_decodes_chunked_txt(monkeypatch):
    resolver = ProductionDnsTxtResolver(timeout_seconds=2.0)

    class _Answer:
        def __iter__(self):
            rdata = MagicMock()
            rdata.strings = [b"pasarguard-site-verification=", b"abc123"]
            return iter([rdata])

    def _resolve(name, rdtype):
        return _Answer()

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", lambda self, name, rdtype: _resolve(name, rdtype))
    values = resolver._lookup_sync(verification_txt_record_name("tenant.example.com"))
    assert values == ["pasarguard-site-verification=abc123"]


@pytest.mark.asyncio
async def test_dns_verification_fail_closed_on_resolver_errors():
    hostname = f"subs-{uuid.uuid4().hex[:6]}.example.com"
    token = "t" * 32
    set_dns_txt_resolver(_FakeResolver(error=DnsTimeout("timeout")))
    assert await dns_verification_matches(hostname, token) is False
    set_dns_txt_resolver(_FakeResolver(error=DnsNxDomain("nxdomain")))
    assert await dns_verification_matches(hostname, token) is False
    set_dns_txt_resolver(_FakeResolver(error=DnsNoData("nodata")))
    assert await dns_verification_matches(hostname, token) is False


@pytest.mark.asyncio
async def test_dns_verification_accepts_expected_txt_among_multiple():
    hostname = f"subs-{uuid.uuid4().hex[:6]}.example.com"
    token = "tok-" + uuid.uuid4().hex
    record = verification_txt_record_name(hostname)
    set_dns_txt_resolver(
        _FakeResolver(
            {
                record: [
                    "v=spf1 include:example.net",
                    verification_txt_record_value(token),
                ]
            }
        )
    )
    assert await dns_verification_matches(hostname, token) is True
    assert await dns_verification_matches(hostname, "wrong") is False


@pytest.mark.asyncio
async def test_certificate_job_rejects_unverified_domain():
    set_certificate_provisioner(StubCertificateProvisioner())
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        row, _ = await create_custom_domain(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant_id,
            hostname_normalized=f"unverified-{uuid.uuid4().hex[:6]}.example.com",
        )
        row.state = SubscriptionCustomDomainState.pending_certificate
        await db.commit()

    await process_custom_domain_certificates()

    async with GetDB() as db:
        refreshed = await get_custom_domain_by_id(db, row.id)
        assert refreshed.state == SubscriptionCustomDomainState.failed


@pytest.mark.asyncio
async def test_certificate_job_idempotent_with_stub():
    set_certificate_provisioner(StubCertificateProvisioner())
    op = CustomDomainOperation()
    hostname = f"subs-{uuid.uuid4().hex[:6]}.example.com"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        created = await op.register_domain(db, CustomDomainCreate(hostname=hostname), details)
        plain_token = created.verification.record_value.split("=", 1)[1]
        set_dns_txt_resolver(
            _FakeResolver(
                {verification_txt_record_name(hostname): [verification_txt_record_value(plain_token)]}
            )
        )
        await op.check_verification(db, created.id, details, token=plain_token)
        await op.request_certificate(db, created.id, details)
        await db.commit()

    await process_custom_domain_certificates()
    await process_custom_domain_certificates()

    async with GetDB() as db:
        final = await get_custom_domain_by_id(db, created.id)
        assert final.state == SubscriptionCustomDomainState.active
        assert final.certificate_ref


class _FlakyProvisioner(StubCertificateProvisioner):
    def __init__(self):
        self.calls = 0

    async def issue(self, domain):
        self.calls += 1
        if self.calls == 1:
            raise CertificateRateLimited("slow down")
        return await super().issue(domain)


@pytest.mark.asyncio
async def test_certificate_job_retries_rate_limits():
    provisioner = _FlakyProvisioner()
    set_certificate_provisioner(provisioner)
    op = CustomDomainOperation()
    hostname = f"subs-{uuid.uuid4().hex[:6]}.example.com"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        created = await op.register_domain(db, CustomDomainCreate(hostname=hostname), details)
        token = created.verification.record_value.split("=", 1)[1]
        set_dns_txt_resolver(
            _FakeResolver({verification_txt_record_name(hostname): [verification_txt_record_value(token)]})
        )
        await op.check_verification(db, created.id, details, token=token)
        await op.request_certificate(db, created.id, details)
        row = await get_custom_domain_by_id(db, created.id)
        row.certificate_issue_locked_until = None
        await db.commit()

    await process_custom_domain_certificates()
    async with GetDB() as db:
        mid = await get_custom_domain_by_id(db, created.id)
        assert mid.state == SubscriptionCustomDomainState.pending_certificate
        mid.certificate_issue_locked_until = None
        await db.commit()

    await process_custom_domain_certificates()
    async with GetDB() as db:
        final = await get_custom_domain_by_id(db, created.id)
        assert final.state == SubscriptionCustomDomainState.active


@pytest.mark.asyncio
async def test_stale_certificate_job_cannot_activate_removed_domain():
    set_certificate_provisioner(StubCertificateProvisioner())
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        row, _ = await create_custom_domain(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant_id,
            hostname_normalized=f"gone-{uuid.uuid4().hex[:6]}.example.com",
        )
        row.state = SubscriptionCustomDomainState.pending_certificate
        row.verified_at = dt.now(UTC)
        domain_id = row.id
        await db.commit()
        await db.delete(row)
        await db.commit()

    await process_custom_domain_certificates()
    async with GetDB() as db:
        assert await get_custom_domain_by_id(db, domain_id) is None


@pytest.mark.asyncio
async def test_tls_permission_endpoint_fail_closed():
    app = create_app()
    client = TestClient(app)
    response = client.get("/internal/caddy-tls-permission", params={"domain": "unknown.example.com"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_tls_permission_allows_verified_pending_certificate():
    hostname = f"perm-{uuid.uuid4().hex[:6]}.example.com"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        row, _ = await create_custom_domain(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant_id,
            hostname_normalized=hostname,
        )
        row.verified_at = dt.now(UTC)
        row.state = SubscriptionCustomDomainState.pending_certificate
        await db.commit()
        allowed = await hostname_may_receive_tls_certificate(db, hostname)
        assert allowed is True


@pytest.mark.asyncio
async def test_expired_certificate_is_not_operational_for_routing():
    from app.services.custom_domain.ssl_lifecycle import certificate_is_operational

    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        row, _ = await create_custom_domain(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant_id,
            hostname_normalized=f"pending-{uuid.uuid4().hex[:6]}.example.com",
        )
        row.state = SubscriptionCustomDomainState.active
        row.verified_at = dt.now(UTC)
        row.certificate_not_after = dt.now(UTC) - td(hours=1)
        await db.commit()
        assert certificate_is_operational(row) is False


@pytest.mark.asyncio
async def test_caddy_provisioner_issue_uses_admin_api():
    domain_row = MagicMock()
    domain_row.id = 42
    domain_row.hostname_normalized = "tenant.example.com"

    class _Resp:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            if self._payload is None:
                return {}
            return self._payload

    responses = {
        ("GET", "/config/apps/http/servers/pasarguard_custom_domains"): _Resp(404),
        ("PUT", "/config/apps/http/servers/pasarguard_custom_domains"): _Resp(200),
        ("GET", "/config/apps/http/servers/pasarguard_custom_domains/routes"): _Resp(200, []),
        ("POST", "/config/apps/http/servers/pasarguard_custom_domains/routes"): _Resp(201),
        ("GET", "/pki/certificates/local/tenant.example.com"): _Resp(
            200, {"expires": (dt.now(UTC) + td(days=60)).isoformat()}
        ),
    }

    class _Client:
        async def request(self, method, path, **kwargs):
            return responses[(method, path)]

        async def aclose(self):
            return None

    provisioner = CaddyCertificateProvisioner("http://127.0.0.1:2019", http_client=_Client())
    ref, not_after = await provisioner.issue(domain_row)
    assert ref == "caddy:tenant.example.com"
    assert not_after > dt.now(UTC)


@pytest.mark.asyncio
async def test_certificate_job_marks_terminal_failure_after_max_attempts():
    class _AlwaysFail(StubCertificateProvisioner):
        async def issue(self, domain):
            raise CertificateProviderUnavailable("down")

    set_certificate_provisioner(_AlwaysFail())
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        row, _ = await create_custom_domain(
            db,
            workspace_id=admin.workspace_id,
            tenant_id=tenant_id,
            hostname_normalized=f"fail-{uuid.uuid4().hex[:6]}.example.com",
        )
        row.state = SubscriptionCustomDomainState.pending_certificate
        row.verified_at = dt.now(UTC)
        row.certificate_issue_locked_until = None
        await db.commit()
        domain_id = row.id

    with patch("app.jobs.custom_domain_ssl.custom_domain_settings") as settings:
        settings.certificate_max_attempts = 2
        settings.certificate_retry_backoff_seconds = 60
        await process_custom_domain_certificates()
        async with GetDB() as db:
            row = await get_custom_domain_by_id(db, domain_id)
            row.certificate_issue_locked_until = None
            await db.commit()
        await process_custom_domain_certificates()

    async with GetDB() as db:
        final = await get_custom_domain_by_id(db, domain_id)
        assert final.state == SubscriptionCustomDomainState.failed
        assert final.certificate_last_error
