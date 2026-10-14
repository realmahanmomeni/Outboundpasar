"""Phase 3-D: per-workspace subscription branding (API compatibility + isolation)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.models.subscription_branding import SubscriptionBrandingUpsert
from app.operation import OperatorType
from app.operation.subscription_branding import SubscriptionBrandingOperation
from app.services.subscription_branding import resolve_branding_page_overlay, resolve_subscription_page_experience
from config import subscription_branding_settings


async def _tenant(db) -> int:
    t = Tenant(name=f"3d_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"3d_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_branding_overlay_none_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(subscription_branding_settings, "enabled", False)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        op = SubscriptionBrandingOperation(OperatorType.API)
        details = build_admin_details(admin, include_loaded_metrics=False)
        await op.create_branding(
            db,
            SubscriptionBrandingUpsert(brand_name="Hidden Brand"),
            details,
        )
        overlay = await resolve_branding_page_overlay(db, workspace_id=admin.workspace_id)
        assert overlay is None
        page = await resolve_subscription_page_experience(db, workspace_id=admin.workspace_id, experience_from_template=None)
        assert page is None


@pytest.mark.asyncio
async def test_workspace_isolation_for_branding_crud():
    op = SubscriptionBrandingOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)
        await op.create_branding(db, SubscriptionBrandingUpsert(brand_name="Workspace A"), details_a)
        with pytest.raises(Exception) as exc:
            await op.get_branding(db, details_b)
        assert getattr(exc.value, "status_code", None) == 404
        with pytest.raises(Exception) as exc2:
            await op.update_branding(db, SubscriptionBrandingUpsert(brand_name="Stolen"), details_b)
        assert getattr(exc2.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_branding_renders_when_enabled(monkeypatch):
    monkeypatch.setattr(subscription_branding_settings, "enabled", True)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        op = SubscriptionBrandingOperation(OperatorType.API)
        details = build_admin_details(admin, include_loaded_metrics=False)
        await op.create_branding(
            db,
            SubscriptionBrandingUpsert(
                brand_name="Acme VPN",
                logo_url="https://example.com/logo.png",
                support_url="https://example.com/support",
                primary_color="200 50% 40%",
            ),
            details,
        )
        page = await resolve_subscription_page_experience(db, workspace_id=admin.workspace_id, experience_from_template=None)
        assert page is not None
        assert page["title"] == "Acme VPN"
        assert page["logo_url"] == "https://example.com/logo.png"


@pytest.mark.asyncio
async def test_missing_branding_falls_back(monkeypatch):
    monkeypatch.setattr(subscription_branding_settings, "enabled", True)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        page = await resolve_subscription_page_experience(db, workspace_id=admin.workspace_id, experience_from_template=None)
        assert page is None


@pytest.mark.asyncio
async def test_invalid_url_rejected():
    with pytest.raises(Exception):
        SubscriptionBrandingUpsert(logo_url="javascript:alert(1)")
