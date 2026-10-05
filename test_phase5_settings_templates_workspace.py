"""Phase 5: settings and templates workspace isolation within a tenant."""
from __future__ import annotations

import uuid
from test_workspace_isolation_util import unique_username, unique_tag
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.settings import ensure_workspace_settings, get_settings_by_workspace
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Group, Tenant, TenantStatus, UserTemplate
from app.models.client_template import ClientTemplateCreate, ClientTemplateListQuery, ClientTemplateType
from app.models.group import GroupCreate
from app.models.settings import SettingsSchema
from app.models.user_template import UserTemplateCreate, UserTemplateListQuery, UserTemplateSimpleListQuery
from app.operation import OperatorType
from app.operation.client_template import ClientTemplateOperation
from app.operation.group import GroupOperation
from app.operation.settings import SettingsOperation
from app.operation.user_template import UserTemplateOperation
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID


async def _tenant(db) -> int:
    t = Tenant(name=f"p5_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
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


async def test_settings_read_update_isolated_per_workspace():
    settings_op = SettingsOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)

        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_b.workspace_id)

        row_a = await get_settings_by_workspace(db, admin_a.workspace_id)
        row_b = await get_settings_by_workspace(db, admin_b.workspace_id)
        assert row_a is not None and row_b is not None
        row_a.subscription = {**row_a.subscription, "profile_title": "Workspace A Title"}
        await db.commit()

        settings_b = await settings_op.get_settings(db, details_b)
        assert settings_b.subscription is not None
        assert settings_b.subscription.profile_title != "Workspace A Title"

        modify = SettingsSchema.model_validate(await settings_op.get_settings(db, details_a))
        modify.subscription = modify.subscription.model_copy(update={"profile_title": "Only A"})
        await settings_op.modify_settings(db, modify, details_a)
        settings_b_after = await settings_op.get_settings(db, details_b)
        assert settings_b_after.subscription.profile_title != "Only A"
        await db.rollback()


async def test_user_template_list_and_idor_same_tenant():
    template_op = UserTemplateOperation(OperatorType.API)
    group_op = GroupOperation(OperatorType.API)
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            details_a = build_admin_details(admin_a, include_loaded_metrics=False)
            details_b = build_admin_details(admin_b, include_loaded_metrics=False)
            g_a = await group_op.create_group(
                db,
                GroupCreate(name=f"ga_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]),
                details_a,
            )
            g_b = await group_op.create_group(
                db,
                GroupCreate(name=f"gb_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]),
                details_b,
            )
            tpl_a = await template_op.create_user_template(
                db,
                UserTemplateCreate(name=f"tpl_a_{uuid.uuid4().hex[:4]}", group_ids=[g_a.id]),
                details_a,
            )
            tpl_b = await template_op.create_user_template(
                db,
                UserTemplateCreate(name=f"tpl_b_{uuid.uuid4().hex[:4]}", group_ids=[g_b.id]),
                details_b,
            )
            list_a = await template_op.get_user_templates(db, UserTemplateListQuery(), details_a)
            ids_a = {t.id for t in list_a}
            assert tpl_a.id in ids_a
            assert tpl_b.id not in ids_a

            simple_resp = await template_op.get_user_templates_simple(
                db, UserTemplateSimpleListQuery(search="tpl_"), details_a
            )
            simple_ids = {t.id for t in simple_resp.templates}
            assert tpl_b.id not in simple_ids

            try:
                await template_op._get_template_with_access(db, tpl_b.id, details_a)
                raise AssertionError("cross-workspace user template must fail")
            except HTTPException as exc:
                assert exc.status_code == 404

            row = await db.get(UserTemplate, tpl_a.id)
            assert row.workspace_id == admin_a.workspace_id
            await db.rollback()


async def test_client_template_workspace_custom_isolation():
    client_op = ClientTemplateOperation(OperatorType.API)
    content = '{"inbounds":[{"tag":"t"}],"outbounds":[{"tag":"o"}]}'
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        details_b = build_admin_details(admin_b, include_loaded_metrics=False)

        custom_a = await client_op.create_client_template(
            db,
            ClientTemplateCreate(
                name=f"custom_{uuid.uuid4().hex[:4]}",
                template_type=ClientTemplateType.xray_subscription,
                content=content,
            ),
            details_a,
        )
        list_b = await client_op.get_client_templates(db, ClientTemplateListQuery(), details_b)
        custom_ids_b = {t.id for t in list_b.templates if not t.is_system}
        assert custom_a.id not in custom_ids_b

        try:
            await client_op._get_template_with_access(db, custom_a.id, details_b)
            raise AssertionError("foreign workspace client template must fail")
        except HTTPException as exc:
            assert exc.status_code == 404
        await db.rollback()


async def test_operator_inherits_workspace_templates_and_settings():
    settings_op = SettingsOperation(OperatorType.API)
    template_op = UserTemplateOperation(OperatorType.API)
    group_op = GroupOperation(OperatorType.API)
    with patch.object(group_op, "check_inbound_tags", new=AsyncMock()):
        async with GetDB() as db:
            tenant_id = await _tenant(db)
            admin_a = await _administrator(db, tenant_id)
            admin_b = await _administrator(db, tenant_id)
            op_a = await _operator(db, tenant_id, admin_a, unique_username("op"))
            details_op = build_admin_details(op_a, include_loaded_metrics=False)
            details_b = build_admin_details(admin_b, include_loaded_metrics=False)

            g_a = await group_op.create_group(
                db,
                GroupCreate(name=f"ga_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]),
                build_admin_details(admin_a, include_loaded_metrics=False),
            )
            tpl_a = await template_op.create_user_template(
                db,
                UserTemplateCreate(name=f"tpl_{uuid.uuid4().hex[:4]}", group_ids=[g_a.id]),
                build_admin_details(admin_a, include_loaded_metrics=False),
            )
            listed = await template_op.get_user_templates(db, UserTemplateListQuery(), details_op)
            assert tpl_a.id in {t.id for t in listed}

            await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
            row = await get_settings_by_workspace(db, admin_a.workspace_id)
            row.subscription = {**row.subscription, "profile_title": "Operator Scope"}
            await db.commit()
            s_op = await settings_op.get_settings(db, details_op)
            assert s_op.subscription.profile_title == "Operator Scope"

            g_b = await group_op.create_group(
                db,
                GroupCreate(name=f"gb_{uuid.uuid4().hex[:4]}", inbound_tags=["VLESS TCP REALITY"]),
                details_b,
            )
            tpl_b = await template_op.create_user_template(
                db,
                UserTemplateCreate(name=f"tpl_b_{uuid.uuid4().hex[:4]}", group_ids=[g_b.id]),
                details_b,
            )
            try:
                await template_op._get_template_with_access(db, tpl_b.id, details_op)
                raise AssertionError("operator must not access other workspace template")
            except HTTPException as exc:
                assert exc.status_code == 404
            await db.rollback()


async def test_legacy_unassigned_user_template_hidden():
    template_op = UserTemplateOperation(OperatorType.API)
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        legacy = UserTemplate(
            name=f"legacy_{uuid.uuid4().hex[:6]}",
            data_limit=0,
            username_prefix=None,
            username_suffix=None,
            extra_settings=None,
            groups=[],
        )
        db.add(legacy)
        await db.flush()
        legacy_id = legacy.id
        try:
            await template_op._get_template_with_access(db, legacy_id, details_a)
            raise AssertionError("legacy template without workspace must not be accessible")
        except HTTPException as exc:
            assert exc.status_code == 404
        listed = await template_op.get_user_templates(db, UserTemplateListQuery(), details_a)
        assert legacy_id not in {t.id for t in listed}
        await db.rollback()


async def run_all():
    await test_settings_read_update_isolated_per_workspace()
    await test_user_template_list_and_idor_same_tenant()
    await test_client_template_workspace_custom_isolation()
    await test_operator_inherits_workspace_templates_and_settings()
    await test_legacy_unassigned_user_template_hidden()
    print("test_phase5_settings_templates_workspace: OK")


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_all())
