from html import escape

from app.models.host import BaseHost
from app.notification.client import send_telegram_message
from app.notification.helpers import get_telegram_channel
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_tg_html

from . import messages
from .utils import escape_html_host

ENTITY = "host"


async def create_host(host: BaseHost, by: str, *, delivery_workspace_id: int | None = None):
    remark, address, tag, by = escape_html_host(host, by)
    data = messages.CREATE_HOST.format(remark=remark, address=address, tag=tag, port=host.port, id=host.id, by=by)
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def modify_host(host: BaseHost, by: str, *, delivery_workspace_id: int | None = None):
    remark, address, tag, by = escape_html_host(host, by)
    data = messages.MODIFY_HOST.format(remark=remark, address=address, tag=tag, port=host.port, id=host.id, by=by)
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def remove_host(host: BaseHost, by: str, *, delivery_workspace_id: int | None = None):
    remark, by = escape_tg_html((host.remark, by))
    data = messages.REMOVE_HOST.format(remark=remark, id=host.id, by=by)
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def modify_hosts(by: str, *, delivery_workspace_id: int | None = None):
    data = messages.MODIFY_HOSTS.format(by=escape(by))
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)
