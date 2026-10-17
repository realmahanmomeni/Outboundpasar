from app.models.admin_role import AdminRoleResponse
from app.notification.client import send_discord_webhook
from app.notification.helpers import get_discord_webhook
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.helpers import escape_ds_markdown_list

from . import colors, messages

ENTITY = "admin_role"


async def create_admin_role(role: AdminRoleResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_ds_markdown_list((role.name, by))
    message = {**messages.CREATE_ADMIN_ROLE, "footer": dict(messages.CREATE_ADMIN_ROLE["footer"])}
    message["description"] = message["description"].format(name=name, is_owner=role.is_owner)
    message["footer"]["text"] = message["footer"]["text"].format(id=role.id, by=by)
    data = {"content": "", "embeds": [message]}
    data["embeds"][0]["color"] = colors.GREEN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def modify_admin_role(role: AdminRoleResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_ds_markdown_list((role.name, by))
    message = {**messages.MODIFY_ADMIN_ROLE, "footer": dict(messages.MODIFY_ADMIN_ROLE["footer"])}
    message["description"] = message["description"].format(name=name, is_owner=role.is_owner)
    message["footer"]["text"] = message["footer"]["text"].format(id=role.id, by=by)
    data = {"content": "", "embeds": [message]}
    data["embeds"][0]["color"] = colors.YELLOW
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)


async def remove_admin_role(role: AdminRoleResponse, by: str, *, delivery_workspace_id: int | None = None):
    name, by = escape_ds_markdown_list((role.name, by))
    message = {**messages.REMOVE_ADMIN_ROLE, "footer": dict(messages.REMOVE_ADMIN_ROLE["footer"])}
    message["description"] = message["description"].format(name=name)
    message["footer"]["text"] = message["footer"]["text"].format(id=role.id, by=by)
    data = {"content": "", "embeds": [message]}
    data["embeds"][0]["color"] = colors.RED
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id)
