"""Resolve workspace subscription page experience for opt-in rendering."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.settings import get_settings_by_workspace
from app.db.crud.subscription_experience_template import get_subscription_experience_template_by_id
from app.models.settings import Subscription
from app.models.subscription_experience_template import experience_context_from_row
from config import subscription_experience_settings


async def active_experience_template_id_for_workspace(db: AsyncSession, workspace_id: int) -> int | None:
    row = await get_settings_by_workspace(db, workspace_id)
    if row is None or not isinstance(row.subscription, dict):
        return None
    sub = Subscription.model_validate(row.subscription)
    return sub.active_experience_template_id


async def resolve_subscription_experience_context(
    db: AsyncSession,
    *,
    workspace_id: int | None,
) -> dict | None:
    if not subscription_experience_settings.enabled or workspace_id is None:
        return None
    template_id = await active_experience_template_id_for_workspace(db, workspace_id)
    if template_id is None:
        return None
    template = await get_subscription_experience_template_by_id(
        db,
        template_id,
        workspace_id=workspace_id,
    )
    if template is None or not template.is_enabled:
        return None
    return experience_context_from_row(template)
