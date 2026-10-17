from html import escape

from app.models.node import NodeNotification, NodeResponse
from app.notification.client import send_telegram_message
from app.notification.helpers import get_telegram_channel
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_tg_html
from app.utils.system import readable_size

from . import messages

ENTITY = "node"


async def create_node(node: NodeResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_tg_html((node.name, by))
    data = messages.CREATE_NODE.format(id=node.id, name=name, address=node.address, port=node.port, by=by)
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def modify_node(node: NodeResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_tg_html((node.name, by))
    data = messages.MODIFY_NODE.format(id=node.id, name=name, address=node.address, port=node.port, by=by)
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def remove_node(node: NodeResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_tg_html((node.name, by))
    data = messages.REMOVE_NODE.format(id=node.id, name=name, by=by)
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def connect_node(node: NodeNotification, *, delivery_workspace_id: int | None = None):
    data = messages.CONNECT_NODE.format(
        name=escape(node.name), node_version=node.node_version, core_version=node.core_version, id=node.id
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def recovered_node(node: NodeNotification, *, delivery_workspace_id: int | None = None):
    data = messages.RECOVERED_NODE.format(
        name=escape(node.name), node_version=node.node_version, core_version=node.core_version, id=node.id
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def error_node(node: NodeNotification, *, delivery_workspace_id: int | None = None):
    name, message = escape_tg_html((node.name, node.message))
    data = messages.ERROR_NODE.format(name=name, error=message, id=node.id)
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def limited_node(node: NodeNotification, data_limit: int, used_traffic: int, *, delivery_workspace_id: int | None = None):
    data = messages.LIMITED_NODE.format(
        name=escape(node.name),
        data_limit=readable_size(data_limit),
        used_traffic=readable_size(used_traffic),
        id=node.id,
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)


async def reset_node_usage(node: NodeResponse, by: str, uplink: int, downlink: int, *, delivery_workspace_id: int | None = None):
    name, by_escaped = escape_tg_html((node.name, by))
    data = messages.RESET_NODE_USAGE.format(
        name=name,
        uplink=readable_size(uplink),
        downlink=readable_size(downlink),
        id=node.id,
        by=by_escaped,
    )
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_telegram:
        chat_id, topic_id = get_telegram_channel(settings, ENTITY)
        await send_telegram_message(data, chat_id, topic_id, delivery_workspace_id=delivery_workspace_id)
