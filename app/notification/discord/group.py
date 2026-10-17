from app.models.group import GroupResponse
from app.notification.client import send_discord_webhook
from app.notification.helpers import get_discord_webhook
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_ds_markdown, escape_ds_markdown_list

from . import colors, messages

ENTITY = "group"


async def create_group(group: GroupResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_ds_markdown_list((group.name, by))
    message = {**messages.CREATE_GROUP, "footer": dict(messages.CREATE_GROUP["footer"])}
    message["description"] = message["description"].format(
        name=name,
        inbound_tags=group.inbound_tags,
        is_disabled=group.is_disabled,
        status="Disabled" if group.is_disabled else "Enabled",
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=group.id, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.GREEN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def modify_group(group: GroupResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_ds_markdown_list((group.name, by))
    message = {**messages.MODIFY_GROUP, "footer": dict(messages.MODIFY_GROUP["footer"])}
    message["description"] = message["description"].format(
        name=name,
        inbound_tags=group.inbound_tags,
        is_disabled=group.is_disabled,
        status="Disabled" if group.is_disabled else "Enabled",
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=group.id, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.YELLOW
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def remove_group(group_id: int, by: str, *, delivery_workspace_id: int | None = None):
    by = escape_ds_markdown(by)
    message = {**messages.REMOVE_GROUP, "footer": dict(messages.REMOVE_GROUP["footer"])}
    message["description"] = message["description"].format(id=group_id)
    message["footer"]["text"] = message["footer"]["text"].format(by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.RED
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)
