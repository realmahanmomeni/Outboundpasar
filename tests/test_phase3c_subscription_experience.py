"""Phase 3-C: subscription experience templates (API compatibility + isolation)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.settings import ensure_workspace_settings
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.models.subscription_experience_template import (
    SubscriptionExperienceActivateRequest,
    SubscriptionExperienceTemplateCreate,
    SubscriptionExperienceTemplateModify,
)
from app.operation import OperatorType
from app.operation.subscription_experience_template import SubscriptionExperienceTemplateOperation
from app.services.subscription_experience import resolve_subscription_experience_context
from config import subscription_experience_settings


async def _tenant(db) -> int:
    t = Tenant(name=f"3c_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"3c_{uuid.uuid4().hex[:12]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_experience_context_none_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(subscription_experience_settings, "enabled", False)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        op = SubscriptionExperienceTemplateOperation(OperatorType.API)
        details = build_admin_details(admin, include_loaded_metrics=False)
        created = await op.create_template(
            db,
            SubscriptionExperienceTemplateCreate(name="disabled-flag", title="Should Not Show"),
            details,
        )
        await op.activate_template(db, SubscriptionExperienceActivateRequest(template_id=created.id), details)
        ctx = await resolve_subscription_experience_context(db, workspace_id=admin.workspace_id)
        assert ctx is None


@pytest.mark.asyncio
async def test_workspace_isolation_for_templates():
    op = SubscriptionExperienceTemplateOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)
        created = await op.create_template(
            db,
            SubscriptionExperienceTemplateCreate(name="workspace-a-only"),
            details_a,
        )
        with pytest.raises(Exception) as exc:
            await op.update_template(
                db,
                created.id,
                SubscriptionExperienceTemplateModify(name="hijack"),
                details_b,
            )
        assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_disabled_template_falls_back(monkeypatch):
    monkeypatch.setattr(subscription_experience_settings, "enabled", True)
    op = SubscriptionExperienceTemplateOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin.workspace_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        row = await op.create_template(
            db,
            SubscriptionExperienceTemplateCreate(name="off", is_enabled=False, title="Off"),
            details,
        )
        from app.models.settings import Subscription

        settings_row = await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin.workspace_id)
        sub = Subscription.model_validate(settings_row.subscription or {})
        settings_row.subscription = sub.model_copy(update={"active_experience_template_id": row.id}).model_dump(
            mode="json"
        )
        await db.commit()
        ctx = await resolve_subscription_experience_context(db, workspace_id=admin.workspace_id)
        assert ctx is None


@pytest.mark.asyncio
async def test_enabled_active_template_resolves_context(monkeypatch):
    monkeypatch.setattr(subscription_experience_settings, "enabled", True)
    op = SubscriptionExperienceTemplateOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        row = await op.create_template(
            db,
            SubscriptionExperienceTemplateCreate(name="live", title="Live Title"),
            details,
        )
        await op.activate_template(db, SubscriptionExperienceActivateRequest(template_id=row.id), details)
        ctx = await resolve_subscription_experience_context(db, workspace_id=admin.workspace_id)
        assert ctx is not None
        assert ctx["title"] == "Live Title"


@pytest.mark.asyncio
async def test_delete_active_template_clears_selection(monkeypatch):
    monkeypatch.setattr(subscription_experience_settings, "enabled", True)
    op = SubscriptionExperienceTemplateOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin.workspace_id)
        details = build_admin_details(admin, include_loaded_metrics=False)
        row = await op.create_template(
            db,
            SubscriptionExperienceTemplateCreate(name="temp", title="Temp"),
            details,
        )
        await op.activate_template(db, SubscriptionExperienceActivateRequest(template_id=row.id), details)
        await op.delete_template(db, row.id, details)
        ctx = await resolve_subscription_experience_context(db, workspace_id=admin.workspace_id)
        assert ctx is None
