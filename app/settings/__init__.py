from aiocache import cached

from app.db import GetDB
from app.db.crud.settings import get_global_settings as get_settings
from app.models import settings
from app.services.runtime_settings import (
    clear_runtime_settings_cache,
    parse_settings_refresh_message,
)


def _legacy_global_key_builder(func, *args, **kwargs) -> str:
    return f"{func.__module__}.{func.__qualname__}:global"


@cached(key_builder=_legacy_global_key_builder)
async def telegram_settings() -> settings.Telegram:
    async with GetDB() as db:
        db_settings = await get_settings(db)

    validated_settings = settings.Telegram.model_validate(db_settings.telegram)
    return validated_settings


@cached(key_builder=_legacy_global_key_builder)
async def webhook_settings() -> settings.Webhook:
    async with GetDB() as db:
        db_settings = await get_settings(db)

    validated_settings = settings.Webhook.model_validate(db_settings.webhook)
    return validated_settings


@cached(key_builder=_legacy_global_key_builder)
async def notification_settings() -> settings.NotificationSettings:
    async with GetDB() as db:
        db_settings = await get_settings(db)

    validated_settings = settings.NotificationSettings.model_validate(db_settings.notification_settings)
    return validated_settings


@cached(key_builder=_legacy_global_key_builder)
async def notification_enable() -> settings.NotificationEnable:
    async with GetDB() as db:
        db_settings = await get_settings(db)

    validated_settings = settings.NotificationEnable.model_validate(db_settings.notification_enable)
    return validated_settings


@cached(key_builder=_legacy_global_key_builder)
async def subscription_settings() -> settings.Subscription:
    async with GetDB() as db:
        db_settings = await get_settings(db)

    validated_settings = settings.Subscription.model_validate(db_settings.subscription)
    return validated_settings


@cached(key_builder=_legacy_global_key_builder)
async def hwid_settings() -> settings.HWIDSettings:
    async with GetDB() as db:
        db_settings = await get_settings(db)

    validated_settings = settings.HWIDSettings.model_validate(db_settings.hwid)
    return validated_settings


@cached(key_builder=_legacy_global_key_builder)
async def general_settings() -> settings.General:
    async with GetDB() as db:
        db_settings = await get_settings(db)

    validated_settings = settings.General.model_validate(db_settings.general)
    return validated_settings


_LEGACY_GLOBAL_ACCESSORS = (
    telegram_settings,
    webhook_settings,
    notification_settings,
    notification_enable,
    subscription_settings,
    hwid_settings,
    general_settings,
)


async def _refresh_legacy_global_section_caches() -> None:
    for accessor in _LEGACY_GLOBAL_ACCESSORS:
        await accessor.cache.clear()


async def refresh_caches(workspace_id: int | None = None, *, sync_telegram_bot: bool = False) -> None:
    """
    Invalidate cached settings.

    ``workspace_id=None``: platform-global legacy section caches and global runtime snapshot.
    ``workspace_id=<int>``: only that workspace's runtime snapshot (legacy global caches unchanged).
    """
    if workspace_id is None:
        await _refresh_legacy_global_section_caches()
        await clear_runtime_settings_cache(workspace_id=None)
    else:
        await clear_runtime_settings_cache(workspace_id=workspace_id)

    if sync_telegram_bot:
        try:
            from app.telegram import telegram_bot_manager
        except Exception:
            return
        await telegram_bot_manager.sync_from_settings()


async def handle_settings_message(payload: dict | None):
    """Handle settings update message from NATS router."""
    event = parse_settings_refresh_message(payload)
    if event is None:
        return

    if event.workspace_id is None:
        await refresh_caches(workspace_id=None, sync_telegram_bot=True)
        return

    await refresh_caches(workspace_id=event.workspace_id, sync_telegram_bot=False)
