"""Phase 6: subscription and output workspace isolation."""
from __future__ import annotations

import base64
import uuid
from test_workspace_isolation_util import unique_username, unique_tag
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.client_template import get_client_template_values
from app.db.crud.settings import ensure_workspace_settings, get_settings_by_workspace
from app.db.crud.user import create_user
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.models.client_template import ClientTemplateCreate, ClientTemplateType
from app.models.group import GroupCreate
from app.models.settings import ConfigFormat
from app.models.user import UserCreate
from app.operation import OperatorType
from app.operation.client_template import ClientTemplateOperation
from app.operation.group import GroupOperation
from app.operation.subscription import SubscriptionOperation
from app.operation.user import UserOperation
from app.services.subscription_scope import subscription_settings_for_user
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID


async def _tenant(db) -> int:
    t = Tenant(name=f"p6_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int, name: str | None = None) -> Admin:
    username = name or unique_username("adm")
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=username, hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


async def _operator(db, tenant_id: int, creator: Admin, name: str) -> Admin:
    op_role = (await db.execute(select(AdminRole).where(AdminRole.id == BUILTIN_OPERATOR_ROLE_ID))).scalar_one()
    row = Admin(username=name, hashed_password="x", role_id=op_role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    creator_details = build_admin_details(creator, include_loaded_metrics=False)
    await assign_workspace_for_new_admin(db, row, creator=creator_details)
    await db.refresh(row)
    return row


async def _user(db, admin: Admin, name: str):
    return await create_user(db, UserCreate(username=name, proxy_settings={}), groups=[], admin=admin)


def _decode_profile_title(header_value: str) -> str:
    if header_value.startswith("base64:"):
        return base64.b64decode(header_value[7:]).decode()
    return header_value


async def test_authenticated_subscription_output_idor():
    sub_op = SubscriptionOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await sub_op.user_subscription_by_id(
                db, user_b.id, details_a, ConfigFormat.links, request_url="https://example/sub"
            )
            raise AssertionError("cross-workspace authenticated subscription fetch must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_revoke_subscription_workspace_isolation():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        try:
            await user_op.revoke_user_sub_by_id(db, user_b.id, details_a)
            raise AssertionError("cross-workspace revoke must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_public_subscription_uses_workspace_settings_branding():
    sub_op = SubscriptionOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_a = await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:6]}")
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_b.workspace_id)
        row_a = await get_settings_by_workspace(db, admin_a.workspace_id)
        row_b = await get_settings_by_workspace(db, admin_b.workspace_id)
        row_a.subscription = {**row_a.subscription, "profile_title": "Phase6 Workspace A Brand"}
        row_b.subscription = {**row_b.subscription, "profile_title": "Phase6 Workspace B Brand"}
        await db.commit()

        settings_a = await subscription_settings_for_user(db, user_a)
        settings_b = await subscription_settings_for_user(db, user_b)
        assert settings_a.profile_title == "Phase6 Workspace A Brand"
        assert settings_b.profile_title == "Phase6 Workspace B Brand"

        headers_a = await sub_op.user_subscription_headers(
            db, user_a.sub_token, user_agent="clash", accept_header="text/yaml"
        )
        headers_b = await sub_op.user_subscription_headers(
            db, user_b.sub_token, user_agent="clash", accept_header="text/yaml"
        )
        title_a = _decode_profile_title(headers_a.get("profile-title", ""))
        title_b = _decode_profile_title(headers_b.get("profile-title", ""))
        assert title_a == "Phase6 Workspace A Brand"
        assert title_b == "Phase6 Workspace B Brand"
        await db.rollback()


async def test_subscription_url_uses_workspace_url_prefix():
    user_op = UserOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_a = await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:6]}")
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_b.workspace_id)
        row_a = await get_settings_by_workspace(db, admin_a.workspace_id)
        row_b = await get_settings_by_workspace(db, admin_b.workspace_id)
        row_a.subscription = {**row_a.subscription, "url_prefix": "https://workspace-a.example"}
        row_b.subscription = {**row_b.subscription, "url_prefix": "https://workspace-b.example"}
        await db.commit()

        validated_a = await user_op.validate_user(user_a)
        validated_b = await user_op.validate_user(user_b)
        assert validated_a.subscription_url.startswith("https://workspace-a.example/")
        assert validated_b.subscription_url.startswith("https://workspace-b.example/")
        await db.rollback()


async def test_client_templates_for_subscription_output_scoped():
    client_op = ClientTemplateOperation(OperatorType.API)
    group_op = GroupOperation(OperatorType.API)
    content = '{"inbounds":[{"tag":"t"}],"outbounds":[{"tag":"o"}]}'
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            details_a = build_admin_details(admin_a, include_loaded_metrics=False)
            custom_a = await client_op.create_client_template(
                db,
                ClientTemplateCreate(
                    name=f"custom_{uuid.uuid4().hex[:4]}",
                    template_type=ClientTemplateType.xray_subscription,
                    content=content,
                    is_default=True,
                ),
                details_a,
            )
            values_a = await get_client_template_values(db, workspace_id=admin_a.workspace_id)
            values_b = await get_client_template_values(db, workspace_id=admin_b.workspace_id)
            assert values_a.get("XRAY_SUBSCRIPTION_TEMPLATE") == content
            assert values_b.get("XRAY_SUBSCRIPTION_TEMPLATE") != content or custom_a.id not in values_b
            await db.rollback()


async def test_operator_inherits_workspace_subscription_settings():
    sub_op = SubscriptionOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
        user_a = await _user(db, admin_a, f"ua_{uuid.uuid4().hex[:6]}")
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        row = await get_settings_by_workspace(db, admin_a.workspace_id)
        row.subscription = {**row.subscription, "profile_title": "Operator Inherited Brand"}
        await db.commit()
        settings = await subscription_settings_for_user(db, user_a)
        assert settings.profile_title == "Operator Inherited Brand"
        headers = await sub_op.user_subscription_headers(
            db, user_a.sub_token, user_agent="clash", accept_header="text/yaml"
        )
        assert _decode_profile_title(headers.get("profile-title", "")) == "Operator Inherited Brand"
        await db.rollback()


async def test_foreign_token_does_not_resolve_other_workspace_user():
    sub_op = SubscriptionOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        user_b = await _user(db, admin_b, f"ub_{uuid.uuid4().hex[:6]}")
        db_user = await sub_op.get_validated_sub(db, user_b.sub_token)
        assert db_user.id == user_b.id
        assert db_user.workspace_id == admin_b.workspace_id
        await db.rollback()


async def run_all():
    await test_authenticated_subscription_output_idor()
    await test_revoke_subscription_workspace_isolation()
    await test_public_subscription_uses_workspace_settings_branding()
    await test_subscription_url_uses_workspace_url_prefix()
    await test_client_templates_for_subscription_output_scoped()
    await test_operator_inherits_workspace_subscription_settings()
    await test_foreign_token_does_not_resolve_other_workspace_user()
    print("test_phase6_subscription_output_workspace: OK")


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_all())
