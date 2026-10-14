from app.models.core import CoreResponse
from app.notification.client import send_discord_webhook
from app.notification.helpers import get_discord_webhook
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_ds_markdown

from . import colors, messages
from .utils import escape_md_core

ENTITY = "core"


async def create_core(core: CoreResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, exclude_inbound_tags, fallbacks_inbound_tags, by = escape_md_core(core, by)
    message = {**messages.CREATE_CORE, "footer": dict(messages.CREATE_CORE["footer"])}
    message["description"] = message["description"].format(
        name=name,
        exclude_inbound_tags=exclude_inbound_tags,
        fallbacks_inbound_tags=fallbacks_inbound_tags,
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=core.id, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.GREEN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def modify_core(core: CoreResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, exclude_inbound_tags, fallbacks_inbound_tags, by = escape_md_core(core, by)
    message = {**messages.MODIFY_CORE, "footer": dict(messages.MODIFY_CORE["footer"])}
    message["description"] = message["description"].format(
        name=name,
        exclude_inbound_tags=exclude_inbound_tags,
        fallbacks_inbound_tags=fallbacks_inbound_tags,
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=core.id, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.YELLOW
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def remove_core(core_id: int, by: str, *, delivery_workspace_id: int | None = None):
    by = escape_ds_markdown(by)
    message = {**messages.REMOVE_CORE, "footer": dict(messages.REMOVE_CORE["footer"])}
    message["description"] = message["description"].format(id=core_id)
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
