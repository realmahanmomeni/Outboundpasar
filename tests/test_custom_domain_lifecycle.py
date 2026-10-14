"""Phase 3-B: custom subscription domains, routing, and SSL lifecycle."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.app_factory import create_app
from app.db import GetDB
from app.db.crud.custom_domain import create_custom_domain, get_custom_domain_by_id
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, SubscriptionCustomDomainState, Tenant, TenantStatus
from app.operation.custom_domain import CustomDomainOperation
from app.models.custom_domain import CustomDomainCreate
from app.db.crud.admin import build_admin_details
from app.services.custom_domain.dns import set_dns_txt_resolver
from app.services.custom_domain.normalization import (
    DomainValidationError,
    normalize_custom_domain_hostname,
    verification_txt_record_name,
    verification_txt_record_value,
)
from app.jobs.custom_domain_ssl import process_custom_domain_certificates


class _FakeResolver:
    def __init__(self, mapping: dict[str, list[str]]):
        self.mapping = mapping

    async def lookup_txt(self, name: str) -> list[str]:
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


def test_normalize_rejects_url_and_wildcard():
    with pytest.raises(DomainValidationError):
        normalize_custom_domain_hostname("https://evil.example/path")
    with pytest.raises(DomainValidationError):
        normalize_custom_domain_hostname("*.example.com")


@pytest.mark.asyncio
async def test_tenant_b_cannot_access_tenant_a_domain():
    op = CustomDomainOperation()
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        row, _token = await create_custom_domain(
            db,
            workspace_id=admin_a.workspace_id,
            tenant_id=tenant_id,
            hostname_normalized=f"subs-a-{uuid.uuid4().hex[:8]}.example.com",
        )
        await db.commit()
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)
        with pytest.raises(Exception):
            await op.get_domain(db, row.id, details_b)


@pytest.mark.asyncio
async def test_verification_and_certificate_lifecycle():
    op = CustomDomainOperation()
    hostname = f"subs-{uuid.uuid4().hex[:6]}.example.com"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        created = await op.register_domain(db, CustomDomainCreate(hostname=hostname), details)
        assert created.state == SubscriptionCustomDomainState.pending_verification
        assert created.verification is not None
        plain_token = created.verification.record_value.split("=", 1)[1]
        set_dns_txt_resolver(
            _FakeResolver(
                {
                    verification_txt_record_name(created.hostname): [
                        verification_txt_record_value(plain_token),
                    ]
                }
            )
        )

        verified = await op.check_verification(db, created.id, details, token=plain_token)
        assert verified.state == SubscriptionCustomDomainState.verified

        requested = await op.request_certificate(db, created.id, details)
        assert requested.state == SubscriptionCustomDomainState.pending_certificate

    await process_custom_domain_certificates()

    async with GetDB() as db:
        details = build_admin_details(admin, include_loaded_metrics=False)
        final = await op.get_domain(db, created.id, details)
        assert final.state == SubscriptionCustomDomainState.active
        assert final.certificate_ref
        assert final.certificate_not_after is not None


@pytest.mark.asyncio
async def test_forged_verification_token_rejected():
    op = CustomDomainOperation()
    hostname = f"subs-{uuid.uuid4().hex[:6]}.example.com"
    set_dns_txt_resolver(_FakeResolver({}))
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        created = await op.register_domain(db, CustomDomainCreate(hostname=hostname), details)
        with pytest.raises(Exception):
            await op.check_verification(db, created.id, details, token="wrong-token-value")


def test_custom_domain_host_middleware_blocks_admin_api():
    app = create_app()
    client = TestClient(app)
    host = "inactive-custom.example.com"
    response = client.get("/api/admin", headers={"Host": host})
    assert response.status_code in {401, 404, 405}
