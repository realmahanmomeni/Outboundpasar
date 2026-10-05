from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Settings
from app.models.settings import SettingsSchema


async def get_global_settings(db: AsyncSession) -> Settings | None:
    """Legacy/platform settings row (workspace_id IS NULL)."""
    return (
        await db.execute(select(Settings).where(Settings.workspace_id.is_(None)).order_by(Settings.id.asc()).limit(1))
    ).scalar_one_or_none()


async def get_settings_by_workspace(db: AsyncSession, workspace_id: int | None) -> Settings | None:
    if workspace_id is None:
        return await get_global_settings(db)
    return (
        await db.execute(select(Settings).where(Settings.workspace_id == workspace_id).limit(1))
    ).scalar_one_or_none()


def _copy_settings_row(source: Settings, *, tenant_id: int | None, workspace_id: int) -> Settings:
    return Settings(
        telegram=dict(source.telegram),
        webhook=dict(source.webhook),
        notification_settings=dict(source.notification_settings),
        notification_enable=dict(source.notification_enable),
        subscription=dict(source.subscription),
        hwid=dict(source.hwid),
        general=dict(source.general),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )


async def ensure_workspace_settings(
    db: AsyncSession,
    *,
    tenant_id: int,
    workspace_id: int,
) -> Settings:
    existing = await get_settings_by_workspace(db, workspace_id)
    if existing is not None:
        return existing

    global_row = await get_global_settings(db)
    if global_row is None:
        raise ValueError("Global settings row is missing; cannot initialize workspace settings")

    db_settings = _copy_settings_row(global_row, tenant_id=tenant_id, workspace_id=workspace_id)
    db.add(db_settings)
    await db.commit()
    await db.refresh(db_settings)
    return db_settings


async def get_settings(
    db: AsyncSession,
    *,
    workspace_id: int | None = None,
    tenant_id: int | None = None,
    ensure_workspace: bool = False,
) -> Settings:
    """
    Resolve settings for a scope.

    workspace_id None selects the global legacy row (Owner/platform).
    When ensure_workspace is True, creates a workspace row from global defaults if missing.
    """
    if workspace_id is None:
        row = await get_global_settings(db)
        if row is None:
            raise ValueError("Settings not configured")
        return row

    row = await get_settings_by_workspace(db, workspace_id)
    if row is not None:
        return row
    if not ensure_workspace:
        raise ValueError("Workspace settings not found")
    if tenant_id is None:
        raise ValueError("tenant_id required to initialize workspace settings")
    return await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=workspace_id)


async def modify_settings(db: AsyncSession, db_setting: Settings, modify: SettingsSchema) -> Settings:
    settings_data = modify.model_dump(exclude_none=True)

    for key, value in settings_data.items():
        setattr(db_setting, key, value)

    await db.commit()
    await db.refresh(db_setting)
    return db_setting
