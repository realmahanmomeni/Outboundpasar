from html import escape

from app.models.group import GroupResponse
from app.notification.client import send_telegram_message
from app.notification.helpers import get_telegram_channel
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_tg_html

from . import messages

ENTITY = "group"


async def create_group(group: GroupResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_tg_html((group.name, by))
    data = messages.CREATE_GROUP.format(
        name=name,
        inbound_tags=group.inbound_tags,
        is_disabled=group.is_disabled,
        status="Disabled" if group.is_disabled else "Enabled",
        id=group.id,
        by=by,
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def modify_group(group: GroupResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_tg_html((group.name, by))
    data = messages.MODIFY_GROUP.format(
        name=name,
        inbound_tags=group.inbound_tags,
        is_disabled=group.is_disabled,
        status="Disabled" if group.is_disabled else "Enabled",
        id=group.id,
        by=by,
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def remove_group(group_id: int, by: str, *, delivery_workspace_id: int | None = None):
    data = messages.REMOVE_GROUP.format(id=group_id, by=escape(by))
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)
