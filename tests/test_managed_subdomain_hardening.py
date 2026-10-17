"""Security hardening for managed subdomain bot API and settings."""
from __future__ import annotations

import uuid

import pytest

from app.db import GetDB
from app.db.crud.custom_domain import get_managed_domain_for_workspace, upsert_managed_subdomain
from app.db.crud.managed_subdomain_binding import upsert_bot_binding
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, SubscriptionCustomDomainState, Tenant, TenantStatus
from app.db.models_oc import OCIntegration, TenantTelegramConnection, TenantTelegramConnectionStatus
from app.models.managed_subdomain_integration import ManagedSubdomainSettingsUpdate
from app.operation.custom_domain import CustomDomainOperation
from app.db.crud.admin import build_admin_details
from app.services.custom_domain.managed_subdomain import ManagedSubdomainValidationError, normalize_managed_subdomain_label
from app.services.custom_domain.subscription_hostname import subscription_url_prefix_for_workspace
from app.services.oc_connection_token_lookup import connection_token_sha256, store_connection_token_digest
from app.utils.crypto import encrypt_secret
from config import custom_domain_settings
from sqlalchemy import select


async def _tenant(db) -> int:
    t = Tenant(name=f"h_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"h_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


async def _active_connection(db, admin: Admin, token: str = "conn-token-test") -> TenantTelegramConnection:
    conn = TenantTelegramConnection(
        tenant_id=admin.tenant_id,
        binding_admin_id=admin.id,
        status=TenantTelegramConnectionStatus.active.value,
        active=True,
        oc_account_id=9001,
        oc_connection_token_encrypted=await encrypt_secret(token),
    )
    await store_connection_token_digest(conn, token)
    db.add(conn)
    await db.flush()
    return conn


def test_reserved_label_rejected():
    with pytest.raises(ManagedSubdomainValidationError):
        normalize_managed_subdomain_label("www")


@pytest.mark.asyncio
async def test_spoofed_workspace_id_not_used_in_settings_claim(monkeypatch):
    monkeypatch.setattr(custom_domain_settings, "ingress_public_https_ready", True)
    label = f"s{uuid.uuid4().hex[:6]}"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await _active_connection(db, admin_a)
        await upsert_bot_binding(
            db,
            tenant_id=tenant_id,
            workspace_id=admin_a.workspace_id,
            binding_admin_id=admin_a.id,
            label=label,
            fqdn=f"{label}.outboundcenter.ir",
            oc_subdomain_id="oc-1",
            oc_account_id=9001,
            idempotency_key=None,
        )
        await db.commit()
        op = CustomDomainOperation()
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)
        with pytest.raises(Exception) as exc:
            await op.update_managed_subdomain_settings(
                db, ManagedSubdomainSettingsUpdate(label=label), details_b
            )
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_url_falls_back_when_serving_not_ready(monkeypatch):
    monkeypatch.setattr(custom_domain_settings, "ingress_public_https_ready", False)
    label = f"u{uuid.uuid4().hex[:6]}"
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
        await db.commit()
        prefix = await subscription_url_prefix_for_workspace(
            db, admin.workspace_id, default_prefix="https://default.example"
        )
        assert prefix == "https://default.example"


@pytest.mark.asyncio
async def test_connection_token_digest_lookup():
    token = f"tok-{uuid.uuid4().hex}"
    assert connection_token_sha256(token) == connection_token_sha256(token)


def _starlette_request(method: str, path: str, headers: dict[str, str], body: bytes):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_hmac_replay_and_bad_signature_rejected(monkeypatch):
    from fastapi import HTTPException
    from app.services.integration_request_security import verify_signed_integration_request
    from tests.helpers.managed_subdomain_signing import signed_headers

    secret = "unit-test-hmac-secret"
    monkeypatch.setattr(custom_domain_settings, "managed_subdomain_bot_hmac_secret", secret)
    path = "/api/integration/managed-subdomains/bindings"
    body = b'{"label":"x","oc_subdomain_id":"oc-1"}'
    headers = signed_headers("POST", path, body, secret)

    async with GetDB() as db:
        req = _starlette_request("POST", path, headers, body)
        await verify_signed_integration_request(req, db, raw_body=body)
        await db.commit()

        req_replay = _starlette_request("POST", path, headers, body)
        with pytest.raises(HTTPException) as exc:
            await verify_signed_integration_request(req_replay, db, raw_body=body)
        assert exc.value.status_code == 409

    bad_headers = dict(headers)
    bad_headers["X-PG-Signature"] = "0" * 64
    async with GetDB() as db:
        req_bad = _starlette_request("POST", path, bad_headers, body)
        with pytest.raises(HTTPException) as exc:
            await verify_signed_integration_request(req_bad, db, raw_body=body)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_bot_binding_idempotency_and_cross_workspace_conflict():
    label = f"i{uuid.uuid4().hex[:6]}"
    idem = uuid.uuid4().hex
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        first = await upsert_bot_binding(
            db,
            tenant_id=tenant_id,
            workspace_id=admin_a.workspace_id,
            binding_admin_id=admin_a.id,
            label=label,
            fqdn=f"{label}.outboundcenter.ir",
            oc_subdomain_id="oc-1",
            oc_account_id=9001,
            idempotency_key=idem,
        )
        second = await upsert_bot_binding(
            db,
            tenant_id=tenant_id,
            workspace_id=admin_a.workspace_id,
            binding_admin_id=admin_a.id,
            label=label,
            fqdn=f"{label}.outboundcenter.ir",
            oc_subdomain_id="oc-1",
            oc_account_id=9001,
            idempotency_key=idem,
        )
        assert first.id == second.id
        with pytest.raises(ValueError):
            await upsert_bot_binding(
                db,
                tenant_id=tenant_id,
                workspace_id=admin_b.workspace_id,
                binding_admin_id=admin_b.id,
                label=label,
                fqdn=f"{label}.outboundcenter.ir",
                oc_subdomain_id="oc-2",
                oc_account_id=9002,
                idempotency_key=None,
            )
        await db.rollback()
