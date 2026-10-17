from app.models.api_key import APIKeyResponse
from app.notification.client import send_discord_webhook
from app.notification.helpers import get_discord_webhook
from app.notification.delivery_scope import notification_settings_for_delivery

from . import colors, messages

ENTITY = "api_key"


async def create_api_key(api_key: APIKeyResponse, admin_username: str, by: str, *, delivery_workspace_id: int | None = None):
    data = messages.CREATE_API_KEY.copy()
    data["description"] = data["description"].format(
        name=api_key.name,
        expire_date=api_key.expire_date or "Never",
    )
    data["footer"]["text"] = data["footer"]["text"].format(
        id=api_key.id,
        admin_username=admin_username,
        by=by,
    )
    data["color"] = colors.GREEN

    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook_url = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook_url, delivery_workspace_id=delivery_workspace_id)


async def modify_api_key(api_key: APIKeyResponse, admin_username: str, by: str, *, delivery_workspace_id: int | None = None):
    data = messages.MODIFY_API_KEY.copy()
    data["description"] = data["description"].format(
        name=api_key.name,
        expire_date=api_key.expire_date or "Never",
        status=api_key.status.value,
    )
    data["footer"]["text"] = data["footer"]["text"].format(
        id=api_key.id,
        admin_username=admin_username,
        by=by,
    )
    data["color"] = colors.BLUE

    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook_url = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook_url, delivery_workspace_id=delivery_workspace_id)


async def remove_api_key(api_key: APIKeyResponse, admin_username: str, by: str, *, delivery_workspace_id: int | None = None):
    data = messages.REMOVE_API_KEY.copy()
    data["description"] = data["description"].format(
        name=api_key.name,
    )
    data["footer"]["text"] = data["footer"]["text"].format(
        id=api_key.id,
        admin_username=admin_username,
        by=by,
    )
    data["color"] = colors.RED

    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook_url = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook_url, delivery_workspace_id=delivery_workspace_id)
