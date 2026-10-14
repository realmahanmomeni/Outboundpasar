from __future__ import annotations

from datetime import UTC, datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SubscriptionBrandingProfile
from app.models.subscription_branding import SubscriptionBrandingUpsert


async def get_branding_for_workspace(db: AsyncSession, workspace_id: int) -> SubscriptionBrandingProfile | None:
    return await db.scalar(
        select(SubscriptionBrandingProfile).where(SubscriptionBrandingProfile.workspace_id == workspace_id)
    )


async def create_branding_profile(
    db: AsyncSession,
    payload: SubscriptionBrandingUpsert,
    *,
    tenant_id: int | None,
    workspace_id: int,
    created_by_admin_id: int | None,
) -> SubscriptionBrandingProfile:
    now = dt.now(UTC)
    row = SubscriptionBrandingProfile(
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        created_by_admin_id=created_by_admin_id,
        brand_name=payload.brand_name,
        logo_url=payload.logo_url,
        support_url=payload.support_url,
        telegram_url=payload.telegram_url,
        welcome_message=payload.welcome_message,
        footer_text=payload.footer_text,
        primary_color=payload.primary_color,
        secondary_color=payload.secondary_color,
        favicon_url=payload.favicon_url,
        custom_css=payload.custom_css,
        support_text=payload.support_text,
        customer_footer=payload.customer_footer,
        hide_platform_branding=bool(payload.hide_platform_branding),
        updated_at=now,
    )
    db.add(row)
    await db.flush()
    return row


async def update_branding_profile(
    db: AsyncSession,
    row: SubscriptionBrandingProfile,
    payload: SubscriptionBrandingUpsert,
) -> SubscriptionBrandingProfile:
    row.brand_name = payload.brand_name
    row.logo_url = payload.logo_url
    row.support_url = payload.support_url
    row.telegram_url = payload.telegram_url
    row.welcome_message = payload.welcome_message
    row.footer_text = payload.footer_text
    row.primary_color = payload.primary_color
    row.secondary_color = payload.secondary_color
    row.favicon_url = payload.favicon_url
    row.custom_css = payload.custom_css
    row.support_text = payload.support_text
    row.customer_footer = payload.customer_footer
    row.hide_platform_branding = bool(payload.hide_platform_branding)
    row.updated_at = dt.now(UTC)
    await db.flush()
    return row


async def delete_branding_profile(db: AsyncSession, row: SubscriptionBrandingProfile) -> None:
    await db.delete(row)
