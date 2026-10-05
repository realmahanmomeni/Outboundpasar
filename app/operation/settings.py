import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.settings import get_settings, modify_settings
from app.db.models import Settings
from app.models.admin import AdminDetails
from app.models.settings import General, SettingsSchema, Subscription
from app.nats.message import MessageTopic
from app.nats.router import router
from app.notification.client import define_client
from app.settings import refresh_caches
from app.services.workspace_scope import resolve_tenant_workspace_scope
from app.telegram import startup_telegram_bot

from . import BaseOperation


class SettingsOperation(BaseOperation):
    @staticmethod
    async def reset_services(old_settings: SettingsSchema, new_settings: SettingsSchema):
        if new_settings.telegram != old_settings.telegram:
            await startup_telegram_bot()
        if old_settings.notification_settings.proxy_url != new_settings.notification_settings.proxy_url:
            await define_client()

    async def _resolve_settings_row(self, db: AsyncSession, admin: AdminDetails, *, ensure: bool) -> Settings:
        if admin.is_owner:
            return await get_settings(db, workspace_id=None)
        tenant_id, workspace_id = await resolve_tenant_workspace_scope(db, admin)
        return await get_settings(
            db,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            ensure_workspace=ensure,
        )

    async def get_settings(self, db: AsyncSession, admin: AdminDetails) -> SettingsSchema:
        db_settings = await self._resolve_settings_row(db, admin, ensure=True)
        return SettingsSchema.model_validate(db_settings)

    async def modify_settings(self, db: AsyncSession, modify: SettingsSchema, admin: AdminDetails) -> SettingsSchema:
        db_settings = await self._resolve_settings_row(db, admin, ensure=True)
        old_settings = SettingsSchema.model_validate(db_settings)

        if modify.general and modify.general.custom_variables is not None:
            subscription = modify.subscription or Subscription.model_validate(db_settings.subscription)
            modify.subscription = subscription.model_copy(update={"custom_variables": modify.general.custom_variables})
            modify.general = modify.general.model_copy(update={"custom_variables": None})

        db_settings = await modify_settings(db, db_settings, modify)
        new_settings = SettingsSchema.model_validate(db_settings)
        if new_settings.general and new_settings.subscription:
            new_settings.general.custom_variables = new_settings.subscription.custom_variables

        await refresh_caches()
        await router.publish(MessageTopic.SETTING, {"action": "refresh"})
        asyncio.create_task(self.reset_services(old_settings, new_settings))

        return new_settings

    async def get_general_settings(self, db: AsyncSession, admin: AdminDetails):
        db_settings = await self._resolve_settings_row(db, admin, ensure=True)
        general = General.model_validate(db_settings.general)
        subscription = Subscription.model_validate(db_settings.subscription)
        return general.model_copy(update={"custom_variables": subscription.custom_variables})
