from datetime import UTC, datetime as dt
from enum import Enum

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from app.models.admin import AdminDetails
from app.models.user import UserNotificationResponse, UserStatus
from app.notification.delivery_scope import USER_SUBJECT_WEBHOOK_ACTIONS, webhook_settings_for_delivery
from app.notification.queue_manager import WebhookScopeAuthorization, enqueue_webhook


def _encoded_payload_has_user_subject(payload: dict) -> bool:
    action = payload.get("action")
    if action not in USER_SUBJECT_WEBHOOK_ACTIONS:
        return False
    user = payload.get("user")
    return isinstance(user, dict) and user.get("id") is not None


def get_current_timestamp() -> float:
    """Factory function to get current timestamp"""
    return dt.now(UTC).timestamp()


class Notification(BaseModel):
    class Type(str, Enum):
        user_created = "user_created"
        user_updated = "user_updated"
        user_deleted = "user_deleted"
        user_limited = "user_limited"
        user_expired = "user_expired"
        user_enabled = "user_enabled"
        user_disabled = "user_disabled"
        data_usage_reset = "data_usage_reset"
        data_reset_by_next = "data_reset_by_next"
        subscription_revoked = "subscription_revoked"

        reached_usage_percent = "reached_usage_percent"
        reached_days_left = "reached_days_left"

    enqueued_at: float = Field(default_factory=get_current_timestamp)
    send_at: float = Field(default_factory=get_current_timestamp)
    tries: int = Field(default=0)


class UserNotification(Notification):
    username: str


class ReachedUsagePercent(UserNotification):
    action: Notification.Type = Notification.Type.reached_usage_percent
    user: UserNotificationResponse
    used_percent: float


class ReachedDaysLeft(UserNotification):
    action: Notification.Type = Notification.Type.reached_days_left
    user: UserNotificationResponse
    days_left: int


class UserCreated(UserNotification):
    action: Notification.Type = Notification.Type.user_created
    by: AdminDetails
    user: UserNotificationResponse


class UserUpdated(UserNotification):
    action: Notification.Type = Notification.Type.user_updated
    by: AdminDetails
    user: UserNotificationResponse


class UserDeleted(UserNotification):
    action: Notification.Type = Notification.Type.user_deleted
    by: AdminDetails
    user: UserNotificationResponse


class UserLimited(UserNotification):
    action: Notification.Type = Notification.Type.user_limited
    user: UserNotificationResponse


class UserExpired(UserNotification):
    action: Notification.Type = Notification.Type.user_expired
    user: UserNotificationResponse


class UserEnabled(UserNotification):
    action: Notification.Type = Notification.Type.user_enabled
    by: AdminDetails | None = None
    user: UserNotificationResponse


class UserDisabled(UserNotification):
    action: Notification.Type = Notification.Type.user_disabled
    by: AdminDetails | None = None
    user: UserNotificationResponse
    reason: str | None = None


class UserDataUsageReset(UserNotification):
    action: Notification.Type = Notification.Type.data_usage_reset
    by: AdminDetails
    user: UserNotificationResponse


class UserDataResetByNext(UserNotification):
    action: Notification.Type = Notification.Type.data_usage_reset
    user: UserNotificationResponse


class UserSubscriptionRevoked(UserNotification):
    action: Notification.Type = Notification.Type.subscription_revoked
    by: AdminDetails
    user: UserNotificationResponse


async def status_change(user: UserNotificationResponse, *, delivery_workspace_id: int | None = None):
    if user.status == UserStatus.limited:
        await notify(UserLimited(username=user.username, user=user), delivery_workspace_id=delivery_workspace_id)
    elif user.status == UserStatus.expired:
        await notify(UserExpired(username=user.username, user=user), delivery_workspace_id=delivery_workspace_id)
    elif user.status == UserStatus.disabled:
        await notify(UserDisabled(username=user.username, user=user), delivery_workspace_id=delivery_workspace_id)
    elif user.status == UserStatus.active or user.status == UserStatus.on_hold:
        await notify(UserEnabled(username=user.username, user=user), delivery_workspace_id=delivery_workspace_id)


async def notify(
    message: Notification,
    *,
    delivery_workspace_id: int | None = None,
    scope_authorization: WebhookScopeAuthorization | None = None,
) -> None:
    if await webhook_settings_for_delivery(delivery_workspace_id) is None:
        return
    if delivery_workspace_id is not None:
        scope_authorization = None
    elif scope_authorization != "global":
        scope_authorization = None
    payload = jsonable_encoder(message)
    if scope_authorization == "global" and _encoded_payload_has_user_subject(payload):
        scope_authorization = None
    await enqueue_webhook(
        payload,
        delivery_workspace_id=delivery_workspace_id,
        scope_authorization=scope_authorization,
    )


async def bulk_notify(
    messages: list[Notification],
    *,
    delivery_workspace_id: int | None = None,
    scope_authorization: WebhookScopeAuthorization | None = None,
) -> None:
    if await webhook_settings_for_delivery(delivery_workspace_id) is None:
        return
    if delivery_workspace_id is not None:
        scope_authorization = None
    elif scope_authorization != "global":
        scope_authorization = None
    for message in messages:
        payload = jsonable_encoder(message)
        item_scope = scope_authorization
        if item_scope == "global" and _encoded_payload_has_user_subject(payload):
            item_scope = None
        await enqueue_webhook(
            payload,
            delivery_workspace_id=delivery_workspace_id,
            scope_authorization=item_scope,
        )
