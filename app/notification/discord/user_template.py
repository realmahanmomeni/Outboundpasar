from app.models.user_template import UserTemplateResponse
from app.notification.client import send_discord_webhook
from app.notification.helpers import get_discord_webhook
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_ds_markdown_list

from . import colors, messages
from .utils import escape_md_template

ENTITY = "user_template"


async def create_user_template(user_tempelate: UserTemplateResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, username_prefix, username_suffix, by = escape_md_template(user_tempelate, by)
    message = {**messages.CREATE_USER_TEMPLATE, "footer": dict(messages.CREATE_USER_TEMPLATE["footer"])}
    message["description"] = message["description"].format(
        name=name,
        data_limit=user_tempelate.data_limit,
        expire_duration=user_tempelate.expire_duration,
        username_prefix=username_prefix,
        username_suffix=username_suffix,
    )
    message["footer"]["text"] = message["footer"]["text"].format(by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.GREEN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def modify_user_template(user_template: UserTemplateResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, username_prefix, username_suffix, by = escape_md_template(user_template, by)
    message = {**messages.MODIFY_USER_TEMPLATE, "footer": dict(messages.MODIFY_USER_TEMPLATE["footer"])}
    message["description"] = message["description"].format(
        name=name,
        data_limit=user_template.data_limit,
        expire_duration=user_template.expire_duration,
        username_prefix=username_prefix,
        username_suffix=username_suffix,
    )
    message["footer"]["text"] = message["footer"]["text"].format(by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.YELLOW
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def remove_user_template(name: str, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_ds_markdown_list((name, by))
    message = {**messages.REMOVE_USER_TEMPLATE, "footer": dict(messages.REMOVE_USER_TEMPLATE["footer"])}
    message["description"] = message["description"].format(name=name)
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
