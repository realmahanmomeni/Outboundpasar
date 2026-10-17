"""Phase 3-E: white-label customer experience — compatibility and isolation."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User
from app.models.subscription_branding import SubscriptionAppearancePreviewRequest, SubscriptionBrandingUpsert
from app.operation import OperatorType
from app.operation.subscription_branding import SubscriptionBrandingOperation
from app.services.subscription_branding import merge_experience_with_branding
from app.services.subscription_customer_experience import (
    resolve_customer_subscription_experience,
    sanitize_experience_for_render,
)
from config import subscription_branding_settings, subscription_experience_settings


async def _tenant(db) -> int:
    t = Tenant(name=f"3e_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"3e_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_flags_disabled_customer_experience_is_none(monkeypatch):
    monkeypatch.setattr(subscription_branding_settings, "enabled", False)
    monkeypatch.setattr(subscription_experience_settings, "enabled", False)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        op = SubscriptionBrandingOperation(OperatorType.API)
        details = build_admin_details(admin, include_loaded_metrics=False)
        await op.create_branding(
            db,
            SubscriptionBrandingUpsert(brand_name="Should Not Render", favicon_url="https://example.com/f.ico"),
            details,
        )
        experience = await resolve_customer_subscription_experience(db, workspace_id=admin.workspace_id)
        assert experience is None


@pytest.mark.asyncio
async def test_branding_enabled_adds_white_label_fields(monkeypatch):
    monkeypatch.setattr(subscription_branding_settings, "enabled", True)
    monkeypatch.setattr(subscription_experience_settings, "enabled", False)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        op = SubscriptionBrandingOperation(OperatorType.API)
        details = build_admin_details(admin, include_loaded_metrics=False)
        await op.create_branding(
            db,
            SubscriptionBrandingUpsert(
                brand_name="Acme",
                favicon_url="https://example.com/favicon.ico",
                support_text="Need help?",
                customer_footer="© Acme",
                hide_platform_branding=True,
            ),
            details,
        )
        experience = await resolve_customer_subscription_experience(db, workspace_id=admin.workspace_id)
        assert experience is not None
        assert experience["title"] == "Acme"
        assert experience["favicon_url"] == "https://example.com/favicon.ico"
        assert experience["support_text"] == "Need help?"
        assert experience["hide_platform_branding"] is True


@pytest.mark.asyncio
async def test_template_wins_over_branding_for_title():
    branding = {
        "title": "Brand",
        "theme": {"primary": "1 1% 1%"},
        "support_links": [{"label": "Support", "url": "https://example.com/s"}],
        "branding": True,
    }
    template = {"title": "Template Title", "logo_url": "https://example.com/logo.png"}
    merged = merge_experience_with_branding(template, branding)
    assert merged["title"] == "Template Title"
    assert merged["logo_url"] == "https://example.com/logo.png"


@pytest.mark.asyncio
async def test_unsafe_custom_css_stripped():
    dirty = {"custom_css": "body{color:red}</style><script>alert(1)</script>", "title": "X"}
    clean = sanitize_experience_for_render(dirty)
    assert clean is not None
    assert "custom_css" not in clean
    assert clean["title"] == "X"


@pytest.mark.asyncio
async def test_workspace_b_cannot_preview_or_read_branding():
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
        preview = await op.preview_customer_page(
            db,
            details_b,
            SubscriptionAppearancePreviewRequest(use_active_template=True),
        )
        assert "Workspace A" not in preview.html


@pytest.mark.asyncio
async def test_user_json_subscription_path_unchanged_flags_off(monkeypatch):
    """Non-HTML subscription clients must not load customer experience layer."""
    monkeypatch.setattr(subscription_branding_settings, "enabled", False)
    monkeypatch.setattr(subscription_experience_settings, "enabled", False)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = User(username=f"u_{uuid.uuid4().hex[:8]}", admin_id=admin.id)
        db.add(user)
        await db.commit()
        experience = await resolve_customer_subscription_experience(db, workspace_id=admin.workspace_id)
        assert experience is None
