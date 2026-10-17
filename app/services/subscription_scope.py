"""Workspace-scoped subscription settings and templates for user-owned subscriptions."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.client_template import (
    get_client_template_contents_by_type,
    get_client_template_values,
)
from app.db.crud.settings import get_global_settings, get_settings_by_workspace
from app.db.models import Admin, User
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


async def resolve_subscription_native_workspace_id(db: AsyncSession, user: User | object) -> int | None:
    """
    Workspace scope for native ``ProxyHost`` subscription output.

    End-user subscriptions are workspace-scoped via ``User.workspace_id`` or the owning admin's
    workspace (loaded relationship or ``admin_id`` DB lookup). ``None`` means native hosts must not
    be emitted (fail closed); it is not global authorization.
    """
    workspace_id = resolve_user_workspace_id(user)
    if workspace_id is not None:
        return workspace_id
    user_id = getattr(user, "id", None)
    if user_id is not None:
        workspace_id = await db.scalar(select(User.workspace_id).where(User.id == user_id))
        if workspace_id is not None:
            return workspace_id
    admin_id = getattr(user, "admin_id", None)
    if admin_id is None and user_id is not None:
        admin_id = await db.scalar(select(User.admin_id).where(User.id == user_id))
    if admin_id is None:
        return None
    return await db.scalar(select(Admin.workspace_id).where(Admin.id == admin_id))


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
