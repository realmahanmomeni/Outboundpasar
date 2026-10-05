"""Workspace-scoped subscription settings and templates for user-owned subscriptions."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.client_template import (
    get_client_template_contents_by_type,
    get_client_template_values,
)
from app.db.crud.settings import get_global_settings, get_settings_by_workspace
from app.db.models import User
from app.models.client_template import ClientTemplateType
from app.models.settings import HWIDSettings, Subscription


def resolve_user_workspace_id(user: User | object) -> int | None:
    """Authoritative workspace for subscription output: User.workspace_id, else owning admin."""
    workspace_id = getattr(user, "workspace_id", None)
    if workspace_id is not None:
        return workspace_id
    admin = getattr(user, "admin", None)
    if admin is not None:
        return getattr(admin, "workspace_id", None)
    return None


async def _settings_row_for_user_workspace(db: AsyncSession, workspace_id: int | None):
    if workspace_id is not None:
        row = await get_settings_by_workspace(db, workspace_id)
        if row is not None:
            return row
    return await get_global_settings(db)


async def subscription_settings_for_user(db: AsyncSession, user: User | object) -> Subscription:
    workspace_id = resolve_user_workspace_id(user)
    row = await _settings_row_for_user_workspace(db, workspace_id)
    if row is None:
        from app.settings import subscription_settings

        return await subscription_settings()
    return Subscription.model_validate(row.subscription)


async def hwid_settings_for_user(db: AsyncSession, user: User | object) -> HWIDSettings:
    workspace_id = resolve_user_workspace_id(user)
    row = await _settings_row_for_user_workspace(db, workspace_id)
    if row is None:
        from app.settings import hwid_settings

        return await hwid_settings()
    return HWIDSettings.model_validate(row.hwid)


async def subscription_client_templates_for_workspace(
    db: AsyncSession,
    workspace_id: int | None,
) -> dict[str, str]:
    return await get_client_template_values(db, workspace_id=workspace_id)


async def subscription_xray_templates_for_workspace(
    db: AsyncSession,
    workspace_id: int | None,
) -> dict[int, str]:
    return await get_client_template_contents_by_type(
        db,
        ClientTemplateType.xray_subscription,
        workspace_id=workspace_id,
    )
