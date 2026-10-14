from app.models.admin_role import AdminRoleResponse
from app.notification.client import send_telegram_message
from app.notification.helpers import get_telegram_channel
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_tg_html

from . import messages

ENTITY = "admin_role"


async def create_admin_role(role: AdminRoleResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_tg_html((role.name, by))
    data = messages.CREATE_ADMIN_ROLE.format(name=name, is_owner=role.is_owner, id=role.id, by=by)
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def modify_admin_role(role: AdminRoleResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_tg_html((role.name, by))
    data = messages.MODIFY_ADMIN_ROLE.format(name=name, is_owner=role.is_owner, id=role.id, by=by)
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def remove_admin_role(role: AdminRoleResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_tg_html((role.name, by))
    data = messages.REMOVE_ADMIN_ROLE.format(name=name, id=role.id, by=by)
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)
