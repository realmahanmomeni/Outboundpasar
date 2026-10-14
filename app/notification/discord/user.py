from app.models.user import UserNotificationResponse
from app.notification.client import send_discord_webhook
from app.notification.helpers import get_discord_webhook, should_send_admin_notification, user_event_queue_scope
from app.notification.delivery_scope import notification_settings_for_delivery
from app.utils.system import readable_size

from . import colors, messages
from .utils import escape_md_user

ENTITY = "user"

_status = {
    "active": "**✅ Activated**",
    "on_hold": "**🕔 On Hold**",
    "disabled": "**❌ Disabled**",
    "limited": "**🪫 Limited**",
    "expired": "**📅 Expired**",
}
_status_color = {
    "active": colors.GREEN,
    "on_hold": colors.PURPLE,
    "disabled": colors.WHITE,
    "limited": colors.RED,
    "expired": colors.YELLOW,
}


async def user_status_change(user: UserNotificationResponse, by: str, *, delivery_workspace_id: int | None = None):
    username, admin_username, by = escape_md_user(user, by)
    message = {**messages.USER_STATUS_CHANGE, "footer": dict(messages.USER_STATUS_CHANGE["footer"])}
    message["title"] = message["title"].format(status=_status[user.status.value])
    message["description"] = message["description"].format(username=username)
    message["footer"]["text"] = message["footer"]["text"].format(admin_username=admin_username, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = _status_color[user.status.value]
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))
    if user.admin and user.admin.discord_webhook and should_send_admin_notification(user.admin, "status_change"):
        await send_discord_webhook(data, user.admin.discord_webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))


async def create_user(user: UserNotificationResponse, by: str, *, delivery_workspace_id: int | None = None):
    username, admin_username, by = escape_md_user(user, by)
    message = {**messages.CREATE_USER, "footer": dict(messages.CREATE_USER["footer"])}
    message["description"] = message["description"].format(
        username=username,
        data_limit=readable_size(user.data_limit) if user.data_limit else "Unlimited",
        expire_date=user.expire if user.expire else "Never",
        data_limit_reset_strategy=user.data_limit_reset_strategy.value,
        groups=", ".join(user.group_names),
        has_next_plan=bool(user.next_plan),
    )
    message["footer"]["text"] = message["footer"]["text"].format(admin_username=admin_username, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.GREEN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))
    if user.admin and user.admin.discord_webhook and should_send_admin_notification(user.admin, "create"):
        await send_discord_webhook(data, user.admin.discord_webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))


async def modify_user(user: UserNotificationResponse, by: str, *, delivery_workspace_id: int | None = None):
    username, admin_username, by = escape_md_user(user, by)
    message = {**messages.MODIFY_USER, "footer": dict(messages.MODIFY_USER["footer"])}
    message["description"] = message["description"].format(
        username=username,
        data_limit=readable_size(user.data_limit) if user.data_limit else "Unlimited",
        expire_date=user.expire if user.expire else "Never",
        data_limit_reset_strategy=user.data_limit_reset_strategy.value,
        groups=", ".join(user.group_names),
        has_next_plan=bool(user.next_plan),
    )
    message["footer"]["text"] = message["footer"]["text"].format(admin_username=admin_username, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.YELLOW
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))
    if user.admin and user.admin.discord_webhook and should_send_admin_notification(user.admin, "modify"):
        await send_discord_webhook(data, user.admin.discord_webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))


async def remove_user(user: UserNotificationResponse, by: str, *, delivery_workspace_id: int | None = None):
    username, admin_username, by = escape_md_user(user, by)
    message = {**messages.REMOVE_USER, "footer": dict(messages.REMOVE_USER["footer"])}
    message["description"] = message["description"].format(username=username)
    message["footer"]["text"] = message["footer"]["text"].format(id=user.id, admin_username=admin_username, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.RED
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))
    if user.admin and user.admin.discord_webhook and should_send_admin_notification(user.admin, "delete"):
        await send_discord_webhook(data, user.admin.discord_webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))


async def reset_user_data_usage(user: UserNotificationResponse, by: str, *, delivery_workspace_id: int | None = None):
    username, admin_username, by = escape_md_user(user, by)
    message = {**messages.RESET_USER_DATA_USAGE, "footer": dict(messages.RESET_USER_DATA_USAGE["footer"])}
    message["description"] = message["description"].format(
        username=username,
        data_limit=readable_size(user.data_limit) if user.data_limit else "Unlimited",
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=user.id, admin_username=admin_username, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.CYAN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))
    if user.admin and user.admin.discord_webhook and should_send_admin_notification(user.admin, "reset_data_usage"):
        await send_discord_webhook(data, user.admin.discord_webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))


async def user_data_reset_by_next(user: UserNotificationResponse, by: str, *, delivery_workspace_id: int | None = None):
    username, admin_username, by = escape_md_user(user, by)
    message = {**messages.USER_DATA_RESET_BY_NEXT, "footer": dict(messages.USER_DATA_RESET_BY_NEXT["footer"])}
    message["description"] = message["description"].format(
        username=username,
        data_limit=readable_size(user.data_limit) if user.data_limit else "Unlimited",
        expire_date=user.expire if user.expire else "Never",
    )
    message["footer"]["text"] = message["footer"]["text"].format(id=user.id, admin_username=admin_username, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.CYAN
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))
    if user.admin and user.admin.discord_webhook and should_send_admin_notification(user.admin, "data_reset_by_next"):
        await send_discord_webhook(data, user.admin.discord_webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))


async def user_subscription_revoked(user: UserNotificationResponse, by: str, *, delivery_workspace_id: int | None = None):
    username, admin_username, by = escape_md_user(user, by)
    message = {**messages.USER_SUBSCRIPTION_REVOKED, "footer": dict(messages.USER_SUBSCRIPTION_REVOKED["footer"])}
    message["description"] = message["description"].format(username=username)
    message["footer"]["text"] = message["footer"]["text"].format(id=user.id, admin_username=admin_username, by=by)
    data = {
        "content": "",
        "embeds": [message],
    }
    data["embeds"][0]["color"] = colors.RED
    settings = await notification_settings_for_delivery(delivery_workspace_id)
    if settings and settings.notify_discord:
        webhook = get_discord_webhook(settings, ENTITY)
        await send_discord_webhook(data, webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))
    if user.admin and user.admin.discord_webhook and should_send_admin_notification(user.admin, "subscription_revoked"):
        await send_discord_webhook(data, user.admin.discord_webhook, delivery_workspace_id=delivery_workspace_id, **user_event_queue_scope(user))
