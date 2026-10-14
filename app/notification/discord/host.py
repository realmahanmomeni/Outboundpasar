from app.models.host import BaseHost
from app.notification.client import send_discord_webhook
from app.notification.helpers import get_discord_webhook
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_ds_markdown, escape_ds_markdown_list

from . import colors, messages
from .utils import escape_md_host

ENTITY = "host"


async def create_host(host: BaseHost, by: str, *, delivery_workspace_id: int | None = None):
    remark, address, inbound_tag, by = escape_md_host(host, by)
    message = {**messages.CREATE_HOST, "footer": dict(messages.CREATE_HOST["footer"])}
    message["description"] = message["description"].format(
        remark=remark, address=address, inbound_tag=inbound_tag, port=host.port
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=host.id, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.GREEN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def modify_host(host: BaseHost, by: str, *, delivery_workspace_id: int | None = None):
    remark, address, inbound_tag, by = escape_md_host(host, by)
    message = {**messages.MODIFY_HOST, "footer": dict(messages.MODIFY_HOST["footer"])}
    message["description"] = message["description"].format(
        remark=remark, address=address, inbound_tag=inbound_tag, port=host.port
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=host.id, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.YELLOW
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def remove_host(host: BaseHost, by: str, *, delivery_workspace_id: int | None = None):
    remark, by = escape_ds_markdown_list((host.remark, by))
    message = {**messages.REMOVE_HOST, "footer": dict(messages.REMOVE_HOST["footer"])}
    message["description"] = message["description"].format(remark=remark)
    message["footer"]["text"] = message["footer"]["text"].format(id=host.id, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.RED
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def modify_hosts(by: str, *, delivery_workspace_id: int | None = None):
    by = escape_ds_markdown(by)
    message = {**messages.MODIFY_HOSTS}
    message["description"] = message["description"].format(by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.CYAN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)
