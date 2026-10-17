from app.models.api_key import APIKeyResponse
from app.notification.client import send_telegram_message
from app.notification.helpers import get_telegram_channel
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_tg_html

from . import messages

ENTITY = "api_key"


async def create_api_key(api_key: APIKeyResponse, admin_username: str, by: str, *, delivery_workspace_id: int | None = None):
    name, admin_username, by = escape_tg_html((api_key.name, admin_username, by))
    data = messages.CREATE_API_KEY.format(
        id=api_key.id,
        name=name,
        expire_date=api_key.expire_date or "Never",
        admin_username=admin_username,
        by=by,
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def modify_api_key(api_key: APIKeyResponse, admin_username: str, by: str, *, delivery_workspace_id: int | None = None):
    name, admin_username, by = escape_tg_html((api_key.name, admin_username, by))
    data = messages.MODIFY_API_KEY.format(
        id=api_key.id,
        name=name,
        expire_date=api_key.expire_date or "Never",
        status=api_key.status.value,
        admin_username=admin_username,
        by=by,
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def remove_api_key(api_key: APIKeyResponse, admin_username: str, by: str, *, delivery_workspace_id: int | None = None):
    name, admin_username, by = escape_tg_html((api_key.name, admin_username, by))
    data = messages.REMOVE_API_KEY.format(
        id=api_key.id,
        name=name,
        admin_username=admin_username,
        by=by,
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)
