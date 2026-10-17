from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.subscription_branding import (
    create_branding_profile,
    delete_branding_profile,
    get_branding_for_workspace,
    update_branding_profile,
)
from app.models.admin import AdminDetails
from app.models.subscription_branding import (
    SubscriptionAppearancePreviewRequest,
    SubscriptionAppearancePreviewResponse,
    SubscriptionBrandingResponse,
    SubscriptionBrandingUpsert,
)
from app.services.subscription_customer_experience import (
    render_customer_subscription_page_html,
    resolve_customer_subscription_experience_for_preview,
)
from app.operation import BaseOperation
from app.services.workspace_scope import require_admin_workspace_id


class SubscriptionBrandingOperation(BaseOperation):
    async def _workspace_scope(self, db: AsyncSession, admin: AdminDetails) -> tuple[int, int]:
        workspace_id = await require_admin_workspace_id(db, admin, admin.is_owner)
        return admin.tenant_id, workspace_id

    async def get_branding(self, db: AsyncSession, admin: AdminDetails) -> SubscriptionBrandingResponse:
        _, workspace_id = await self._workspace_scope(db, admin)
        row = await get_branding_for_workspace(db, workspace_id)
        if row is None:
            await self.raise_error("Subscription branding not found", 404)
        return SubscriptionBrandingResponse.from_db(row)

    async def create_branding(
        self,
        db: AsyncSession,
        payload: SubscriptionBrandingUpsert,
        admin: AdminDetails,
    ) -> SubscriptionBrandingResponse:
        tenant_id, workspace_id = await self._workspace_scope(db, admin)
        existing = await get_branding_for_workspace(db, workspace_id)
        if existing is not None:
            await self.raise_error("Subscription branding already exists for this workspace", 409)
        try:
            row = await create_branding_profile(
                db,
                payload,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                created_by_admin_id=admin.id,
            )
            await db.commit()
            await db.refresh(row)
        except IntegrityError:
            await self.raise_error("Subscription branding already exists for this workspace", 409, db=db)
        return SubscriptionBrandingResponse.from_db(row)

    async def update_branding(
        self,
        db: AsyncSession,
        payload: SubscriptionBrandingUpsert,
        admin: AdminDetails,
    ) -> SubscriptionBrandingResponse:
        _, workspace_id = await self._workspace_scope(db, admin)
        row = await get_branding_for_workspace(db, workspace_id)
        if row is None:
            await self.raise_error("Subscription branding not found", 404)
        row = await update_branding_profile(db, row, payload)
        await db.commit()
        await db.refresh(row)
        return SubscriptionBrandingResponse.from_db(row)

    async def delete_branding(self, db: AsyncSession, admin: AdminDetails) -> None:
        _, workspace_id = await self._workspace_scope(db, admin)
        row = await get_branding_for_workspace(db, workspace_id)
        if row is None:
            await self.raise_error("Subscription branding not found", 404)
        await delete_branding_profile(db, row)
        await db.commit()

    async def preview_customer_page(
        self,
        db: AsyncSession,
        admin: AdminDetails,
        payload: SubscriptionAppearancePreviewRequest,
    ) -> SubscriptionAppearancePreviewResponse:
        _, workspace_id = await self._workspace_scope(db, admin)
        draft: dict[str, Any] | None = None
        if not payload.use_active_template:
            theme = {}
            if payload.theme_primary:
                theme["primary"] = payload.theme_primary
            draft = {
                "title": payload.title,
                "description": payload.description,
                "logo_url": payload.logo_url,
                "footer_text": payload.footer_text,
                "theme": theme,
                "support_links": [],
                "custom_blocks": [],
            }
        experience = await resolve_customer_subscription_experience_for_preview(
            db,
            workspace_id=workspace_id,
            use_active_template=payload.use_active_template,
            template_draft=draft,
        )
        html = render_customer_subscription_page_html(experience)
        return SubscriptionAppearancePreviewResponse(html=html)
