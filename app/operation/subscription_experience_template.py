from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.settings import ensure_workspace_settings, get_settings_by_workspace
from app.db.crud.subscription_experience_template import (
    create_subscription_experience_template,
    delete_subscription_experience_template,
    get_subscription_experience_template_by_id,
    list_subscription_experience_templates,
    modify_subscription_experience_template,
)
from app.models.admin import AdminDetails
from app.models.settings import Subscription
from app.models.subscription_experience_template import (
    SubscriptionExperienceActivateRequest,
    SubscriptionExperiencePreviewRequest,
    SubscriptionExperiencePreviewResponse,
    SubscriptionExperienceTemplateCreate,
    SubscriptionExperienceTemplateListResponse,
    SubscriptionExperienceTemplateModify,
    SubscriptionExperienceTemplateResponse,
    experience_context_from_row,
)
from app.operation import BaseOperation
from app.services.subscription_experience import active_experience_template_id_for_workspace
from app.services.workspace_scope import require_admin_workspace_id
from app.templates import render_template
from config import subscription_experience_settings


class SubscriptionExperienceTemplateOperation(BaseOperation):
    async def _workspace_scope(self, db: AsyncSession, admin: AdminDetails) -> tuple[int, int]:
        tenant_id = admin.tenant_id
        workspace_id = await require_admin_workspace_id(db, admin, admin.is_owner)
        return tenant_id, workspace_id

    async def _active_template_id(self, db: AsyncSession, workspace_id: int) -> int | None:
        return await active_experience_template_id_for_workspace(db, workspace_id)

    async def _get_row(self, db: AsyncSession, template_id: int, admin: AdminDetails):
        _, workspace_id = await self._workspace_scope(db, admin)
        row = await get_subscription_experience_template_by_id(db, template_id, workspace_id=workspace_id)
        if row is None:
            await self.raise_error("Subscription template not found", 404)
        return row, workspace_id

    async def list_templates(self, db: AsyncSession, admin: AdminDetails) -> SubscriptionExperienceTemplateListResponse:
        _, workspace_id = await self._workspace_scope(db, admin)
        active_id = await self._active_template_id(db, workspace_id)
        rows = await list_subscription_experience_templates(db, workspace_id=workspace_id)
        return SubscriptionExperienceTemplateListResponse(
            templates=[
                SubscriptionExperienceTemplateResponse.from_db(row, active_template_id=active_id) for row in rows
            ]
        )

    async def create_template(
        self,
        db: AsyncSession,
        payload: SubscriptionExperienceTemplateCreate,
        admin: AdminDetails,
    ) -> SubscriptionExperienceTemplateResponse:
        tenant_id, workspace_id = await self._workspace_scope(db, admin)
        try:
            row = await create_subscription_experience_template(
                db,
                payload,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            await db.commit()
            await db.refresh(row)
        except IntegrityError:
            await self.raise_error("Template by this name already exists", 409, db=db)
        active_id = await self._active_template_id(db, workspace_id)
        return SubscriptionExperienceTemplateResponse.from_db(row, active_template_id=active_id)

    async def update_template(
        self,
        db: AsyncSession,
        template_id: int,
        payload: SubscriptionExperienceTemplateModify,
        admin: AdminDetails,
    ) -> SubscriptionExperienceTemplateResponse:
        row, workspace_id = await self._get_row(db, template_id, admin)
        try:
            row = await modify_subscription_experience_template(db, row, payload)
            await db.commit()
            await db.refresh(row)
        except IntegrityError:
            await self.raise_error("Template by this name already exists", 409, db=db)
        active_id = await self._active_template_id(db, workspace_id)
        return SubscriptionExperienceTemplateResponse.from_db(row, active_template_id=active_id)

    async def delete_template(self, db: AsyncSession, template_id: int, admin: AdminDetails) -> None:
        row, workspace_id = await self._get_row(db, template_id, admin)
        active_id = await self._active_template_id(db, workspace_id)
        if active_id == row.id:
            await self._set_active_template_id(db, workspace_id, None, admin.tenant_id)
        await delete_subscription_experience_template(db, row)
        await db.commit()

    async def _set_active_template_id(
        self,
        db: AsyncSession,
        workspace_id: int,
        template_id: int | None,
        tenant_id: int,
    ) -> None:
        settings_row = await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=workspace_id)
        subscription = Subscription.model_validate(settings_row.subscription or {})
        updated = subscription.model_copy(update={"active_experience_template_id": template_id})
        settings_row.subscription = updated.model_dump(mode="json")
        await db.flush()

    async def activate_template(
        self,
        db: AsyncSession,
        payload: SubscriptionExperienceActivateRequest,
        admin: AdminDetails,
    ) -> SubscriptionExperienceTemplateListResponse:
        tenant_id, workspace_id = await self._workspace_scope(db, admin)
        if payload.template_id is not None:
            row = await get_subscription_experience_template_by_id(
                db,
                payload.template_id,
                workspace_id=workspace_id,
            )
            if row is None:
                await self.raise_error("Subscription template not found", 404)
            if not row.is_enabled:
                await self.raise_error("Cannot activate a disabled template", 409)
        await self._set_active_template_id(db, workspace_id, payload.template_id, tenant_id)
        await db.commit()
        return await self.list_templates(db, admin)

    async def preview_template(
        self,
        payload: SubscriptionExperiencePreviewRequest,
    ) -> SubscriptionExperiencePreviewResponse:
        if not subscription_experience_settings.enabled:
            await self.raise_error("Subscription experience templates are disabled on this deployment", 503)
        fake_row = type(
            "PreviewRow",
            (),
            {
                "title": payload.title,
                "description": payload.description,
                "logo_url": payload.logo_url,
                "theme": payload.theme.model_dump(exclude_none=True) if payload.theme else {},
                "support_links": [link.model_dump() for link in payload.support_links],
                "footer_text": payload.footer_text,
                "custom_blocks": [block.model_dump() for block in payload.custom_blocks],
            },
        )()
        experience = experience_context_from_row(fake_row)
        html = render_template(
            "subscription/experience_preview.html",
            {
                "experience": experience,
                "user": {"username": "preview-user"},
            },
        )
        return SubscriptionExperiencePreviewResponse(html=html)
