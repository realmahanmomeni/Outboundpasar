from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SubscriptionExperienceTemplate
from app.models.subscription_experience_template import (
    SubscriptionExperienceTemplateCreate,
    SubscriptionExperienceTemplateModify,
)


async def list_subscription_experience_templates(
    db: AsyncSession,
    *,
    workspace_id: int,
) -> list[SubscriptionExperienceTemplate]:
    return list(
        (
            await db.scalars(
                select(SubscriptionExperienceTemplate)
                .where(SubscriptionExperienceTemplate.workspace_id == workspace_id)
                .order_by(SubscriptionExperienceTemplate.id.asc())
            )
        ).all()
    )


async def get_subscription_experience_template_by_id(
    db: AsyncSession,
    template_id: int,
    *,
    workspace_id: int,
) -> SubscriptionExperienceTemplate | None:
    return await db.scalar(
        select(SubscriptionExperienceTemplate).where(
            SubscriptionExperienceTemplate.id == template_id,
            SubscriptionExperienceTemplate.workspace_id == workspace_id,
        )
    )


async def create_subscription_experience_template(
    db: AsyncSession,
    payload: SubscriptionExperienceTemplateCreate,
    *,
    tenant_id: int | None,
    workspace_id: int,
) -> SubscriptionExperienceTemplate:
    row = SubscriptionExperienceTemplate(
        name=payload.name.strip(),
        is_enabled=payload.is_enabled,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        title=payload.title,
        description=payload.description,
        logo_url=payload.logo_url,
        theme=payload.theme.model_dump(exclude_none=True) if payload.theme else None,
        support_links=[link.model_dump() for link in payload.support_links] or None,
        footer_text=payload.footer_text,
        custom_blocks=[block.model_dump() for block in payload.custom_blocks] or None,
    )
    db.add(row)
    await db.flush()
    return row


async def modify_subscription_experience_template(
    db: AsyncSession,
    row: SubscriptionExperienceTemplate,
    payload: SubscriptionExperienceTemplateModify,
) -> SubscriptionExperienceTemplate:
    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.is_enabled is not None:
        row.is_enabled = payload.is_enabled
    if payload.title is not None:
        row.title = payload.title
    if payload.description is not None:
        row.description = payload.description
    if payload.logo_url is not None:
        row.logo_url = payload.logo_url
    if payload.theme is not None:
        row.theme = payload.theme.model_dump(exclude_none=True)
    if payload.support_links is not None:
        row.support_links = [link.model_dump() for link in payload.support_links]
    if payload.footer_text is not None:
        row.footer_text = payload.footer_text
    if payload.custom_blocks is not None:
        row.custom_blocks = [block.model_dump() for block in payload.custom_blocks]
    await db.flush()
    return row


async def delete_subscription_experience_template(db: AsyncSession, row: SubscriptionExperienceTemplate) -> None:
    await db.delete(row)
