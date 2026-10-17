from html import escape

from app.models.core import CoreResponse
from app.notification.client import send_telegram_message
from app.notification.helpers import get_telegram_channel
from app.notification.delivery_scope import notification_settings_for_delivery

from . import messages
from .utils import escape_html_core

ENTITY = "core"


async def create_core(core: CoreResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, exclude_tags, fallback_tags, by = escape_html_core(core, by)
    data = messages.CREATE_CORE.format(
        name=name, exclude_inbound_tags=exclude_tags, fallbacks_inbound_tags=fallback_tags, id=core.id, by=by
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def modify_core(core: CoreResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, exclude_tags, fallback_tags, by = escape_html_core(core, by)
    data = messages.MODIFY_CORE.format(
        name=name,
        exclude_inbound_tags=exclude_tags,
        fallbacks_inbound_tags=fallback_tags,
        id=core.id,
        by=by,
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def remove_core(core_id: int, by: str, *, delivery_workspace_id: int | None = None):
    data = messages.REMOVE_CORE.format(id=core_id, by=escape(by))
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)
