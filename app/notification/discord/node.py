from app.models.node import NodeNotification, NodeResponse
from app.notification.client import send_discord_webhook
from app.notification.helpers import get_discord_webhook
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_ds_markdown, escape_ds_markdown_list
from app.utils.system import readable_size

from . import colors, messages

ENTITY = "node"


async def create_node(node: NodeResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_ds_markdown_list((node.name, by))
    message = {**messages.CREATE_NODE, "footer": dict(messages.CREATE_NODE["footer"])}
    message["description"] = message["description"].format(name=name, address=node.address, port=node.port)
    message["footer"]["text"] = message["footer"]["text"].format(id=node.id, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.GREEN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def modify_node(node: NodeResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_ds_markdown_list((node.name, by))
    message = {**messages.MODIFY_NODE, "footer": dict(messages.MODIFY_NODE["footer"])}
    message["description"] = message["description"].format(name=name, address=node.address, port=node.port)
    message["footer"]["text"] = message["footer"]["text"].format(id=node.id, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.YELLOW
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def remove_node(node: NodeResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_ds_markdown_list((node.name, by))
    message = {**messages.REMOVE_NODE, "footer": dict(messages.REMOVE_NODE["footer"])}
    message["description"] = message["description"].format(name=name, address=node.address, port=node.port)
    message["footer"]["text"] = message["footer"]["text"].format(id=node.id, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.RED
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def connect_node(node: NodeNotification, *, delivery_workspace_id: int | None = None):
    name = escape_ds_markdown(node.name)
    message = {**messages.CONNECT_NODE, "footer": dict(messages.CONNECT_NODE["footer"])}
    message["description"] = message["description"].format(
        name=name, node_version=node.node_version, core_version=node.core_version
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=node.id)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.GREEN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def recovered_node(node: NodeNotification, *, delivery_workspace_id: int | None = None):
    name = escape_ds_markdown(node.name)
    message = {**messages.RECOVERED_NODE, "footer": dict(messages.RECOVERED_NODE["footer"])}
    message["description"] = message["description"].format(
        name=name, node_version=node.node_version, core_version=node.core_version
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=node.id)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.GREEN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def error_node(node: NodeNotification, *, delivery_workspace_id: int | None = None):
    name, node_message = escape_ds_markdown_list((node.name, node.message))
    message = {**messages.ERROR_NODE, "footer": dict(messages.ERROR_NODE["footer"])}
    message["description"] = message["description"].format(name=name, error=node_message)
    message["footer"]["text"] = message["footer"]["text"].format(id=node.id)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.RED
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def limited_node(node: NodeNotification, data_limit: int, used_traffic: int, *, delivery_workspace_id: int | None = None):
    name = escape_ds_markdown(node.name)
    message = {**messages.LIMITED_NODE, "footer": dict(messages.LIMITED_NODE["footer"])}
    message["description"] = message["description"].format(
        name=name, data_limit=readable_size(data_limit), used_traffic=readable_size(used_traffic)
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=node.id)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.YELLOW
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def reset_node_usage(node: NodeResponse, by: str, uplink: int, downlink: int, *, delivery_workspace_id: int | None = None):
    name, by_escaped = escape_ds_markdown_list((node.name, by))
    message = {**messages.RESET_NODE_USAGE, "footer": dict(messages.RESET_NODE_USAGE["footer"])}
    message["description"] = message["description"].format(
        name=name, uplink=readable_size(uplink), downlink=readable_size(downlink)
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=node.id, by=by_escaped)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.BLUE
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)
