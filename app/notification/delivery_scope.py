"""Workspace scope resolution for event-driven Telegram/Discord notifications."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import get_admin
from app.models.admin import AdminDetails, AdminContactInfo, AdminRoleData
from app.models.notification_enable import NotificationEnable
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import NotificationSettings, SettingsSchema, Webhook
from app.models.user import UserNotificationResponse
from app.services.runtime_settings import (
    WorkspaceSettingsNotFoundError,
    get_runtime_settings_snapshot,
)
from app.utils.logger import get_logger

SkipDelivery = Literal["skip"]

logger = get_logger("Notification")

# Serialized ``action`` values for webhook payloads that always carry a user subject.
USER_SUBJECT_WEBHOOK_ACTIONS = frozenset(
    {
        "user_created",
        "user_updated",
        "user_deleted",
        "user_limited",
        "user_expired",
        "user_enabled",
        "user_disabled",
        "data_usage_reset",
        "data_reset_by_next",
        "subscription_revoked",
        "reached_usage_percent",
        "reached_days_left",
    }
)


@dataclass(frozen=True, slots=True)
class EventDeliveryScope:
    """Resolved workspace scope and enable flags for one notification event."""

    workspace_id: int | None
    notification_settings: NotificationSettings
    notification_enable: NotificationEnable


def _admin_workspace_id(admin: AdminContactInfo | AdminDetails | None) -> int | None:
    if admin is None:
        return None
    return getattr(admin, "workspace_id", None)


def resolve_admin_subject_workspace_id(admin: AdminDetails) -> int | None | SkipDelivery:
    if admin.is_owner:
        return None
    if admin.workspace_id is not None:
        return admin.workspace_id
    return "skip"


@dataclass(frozen=True, slots=True)
class _UserOwnerScope:
    """Authoritative user ownership row from the database join."""

    user_workspace_id: int | None
    user_workspace_tenant_id: int | None
    user_admin_id: int | None
    linked_admin_id: int | None
    admin_tenant_id: int | None
    admin_workspace_id: int | None
    admin_is_owner: bool


async def _lookup_user_owner_scope(user_id: int) -> _UserOwnerScope | None:
    from app.db.models import Admin, AdminRole, User, Workspace

    async with GetDB() as db:
        row = (
            await db.execute(
                select(
                    User.workspace_id,
                    Workspace.tenant_id,
                    User.admin_id,
                    Admin.id,
                    Admin.tenant_id,
                    Admin.workspace_id,
                    AdminRole.is_owner,
                )
                .select_from(User)
                .outerjoin(Workspace, User.workspace_id == Workspace.id)
                .outerjoin(Admin, User.admin_id == Admin.id)
                .outerjoin(AdminRole, Admin.role_id == AdminRole.id)
                .where(User.id == user_id)
            )
        ).one_or_none()
    if row is None:
        return None
    admin_is_owner = bool(row[6]) if row[6] is not None else False
    return _UserOwnerScope(
        user_workspace_id=row[0],
        user_workspace_tenant_id=row[1],
        user_admin_id=row[2],
        linked_admin_id=row[3],
        admin_tenant_id=row[4],
        admin_workspace_id=row[5],
        admin_is_owner=admin_is_owner,
    )


def _dto_admin_tenant_id(admin: AdminContactInfo | AdminDetails | None) -> int | None:
    if admin is None:
        return None
    return getattr(admin, "tenant_id", None)


def _dto_conflicts_with_db(
    db_scope: _UserOwnerScope,
    *,
    dto_admin_tenant: int | None,
    dto_admin_workspace: int | None,
) -> bool:
    if dto_admin_tenant is not None:
        if db_scope.admin_tenant_id is not None and dto_admin_tenant != db_scope.admin_tenant_id:
            return True
        if db_scope.admin_tenant_id is None and db_scope.linked_admin_id is not None:
            return True
    if (
        dto_admin_workspace is not None
        and db_scope.admin_workspace_id is not None
        and dto_admin_workspace != db_scope.admin_workspace_id
    ):
        return True
    return False


def _user_workspace_consistent_with_admin(db_scope: _UserOwnerScope) -> bool:
    if db_scope.user_workspace_id is None:
        return True
    if db_scope.user_admin_id is not None and db_scope.linked_admin_id is None:
        return False
    if db_scope.admin_tenant_id is None:
        return True
    if (
        db_scope.admin_workspace_id is not None
        and db_scope.user_workspace_id != db_scope.admin_workspace_id
    ):
        return False
    if (
        db_scope.user_workspace_tenant_id is not None
        and db_scope.user_workspace_tenant_id != db_scope.admin_tenant_id
    ):
        return False
    return True


def _resolve_user_owner_scope_from_db(
    db_scope: _UserOwnerScope,
    *,
    dto_admin_tenant: int | None,
    dto_admin_workspace: int | None,
) -> int | None | SkipDelivery:
    if _dto_conflicts_with_db(
        db_scope,
        dto_admin_tenant=dto_admin_tenant,
        dto_admin_workspace=dto_admin_workspace,
    ):
        return "skip"

    if db_scope.user_workspace_id is not None:
        if not _user_workspace_consistent_with_admin(db_scope):
            return "skip"
        return db_scope.user_workspace_id

    if db_scope.user_admin_id is None:
        return "skip"

    if db_scope.linked_admin_id is None:
        return "skip"

    if db_scope.admin_tenant_id is not None:
        if db_scope.admin_workspace_id is not None:
            return db_scope.admin_workspace_id
        return "skip"

    if db_scope.admin_workspace_id is not None:
        return db_scope.admin_workspace_id

    if db_scope.admin_is_owner:
        return None

    return "skip"


async def resolve_user_event_workspace_id(
    user: UserNotificationResponse,
    by: AdminDetails,
) -> int | None | SkipDelivery:
    """
    Resolve delivery scope from the user subject's ownership only.

    ``None`` means verified platform-global subject ownership (owner admin link),
    not an unresolved tenant user. Actor privilege must not select global scope.
    """
    del by  # actor is not used for subject scope

    dto_admin_workspace = _admin_workspace_id(user.admin)
    dto_admin_tenant = _dto_admin_tenant_id(user.admin)

    if user.id is None:
        return "skip"

    try:
        db_scope = await _lookup_user_owner_scope(user.id)
    except Exception as err:
        logger.warning(
            "Failed to resolve notification ownership for user id=%s: %s",
            user.id,
            err,
            exc_info=True,
        )
        return "skip"

    if db_scope is None:
        return "skip"

    return _resolve_user_owner_scope_from_db(
        db_scope,
        dto_admin_tenant=dto_admin_tenant,
        dto_admin_workspace=dto_admin_workspace,
    )


async def resolve_actor_username_workspace_id(actor_username: str) -> int | None | SkipDelivery:
    if actor_username == "system":
        return None
    async with GetDB() as db:
        db_admin = await get_admin(db, actor_username, load_users=False, load_usage_logs=False)
    if db_admin is None:
        return "skip"
    from app.db.crud.admin import build_admin_details

    details = build_admin_details(db_admin, include_loaded_metrics=False)
    return resolve_admin_subject_workspace_id(details)


async def load_event_delivery_scope(workspace_id: int | None) -> EventDeliveryScope | None:
    try:
        snapshot = await get_runtime_settings_snapshot(workspace_id)
    except (WorkspaceSettingsNotFoundError, ValueError):
        return None
    notification_settings = snapshot.settings.notification_settings
    notification_enable = snapshot.settings.notification_enable
    if notification_settings is None or notification_enable is None:
        return None
    return EventDeliveryScope(
        workspace_id=workspace_id,
        notification_settings=notification_settings,
        notification_enable=notification_enable,
    )


async def scope_for_user_event(user: UserNotificationResponse, by: AdminDetails) -> EventDeliveryScope | None:
    resolved = await resolve_user_event_workspace_id(user, by)
    if resolved == "skip":
        return None
    return await load_event_delivery_scope(resolved)


async def scope_for_admin_subject(admin: AdminDetails) -> EventDeliveryScope | None:
    resolved = resolve_admin_subject_workspace_id(admin)
    if resolved == "skip":
        return None
    return await load_event_delivery_scope(resolved)


async def scope_for_actor_username(actor_username: str) -> EventDeliveryScope | None:
    resolved = await resolve_actor_username_workspace_id(actor_username)
    if resolved == "skip":
        return None
    return await load_event_delivery_scope(resolved)


async def scope_for_admin_username(admin_username: str) -> EventDeliveryScope | None:
    return await scope_for_actor_username(admin_username)


async def notification_settings_for_delivery(workspace_id: int | None) -> NotificationSettings | None:
    scope = await load_event_delivery_scope(workspace_id)
    return scope.notification_settings if scope else None


async def _resolve_user_subject_workspace_from_payload(payload: dict) -> int | None | SkipDelivery | Literal["no_user"]:
    user_data = payload.get("user")
    if not isinstance(user_data, dict) or user_data.get("id") is None:
        return "no_user"
    action = payload.get("action")
    try:
        user = UserNotificationResponse.model_validate(user_data)
    except Exception:
        logger.warning("Skipping webhook queue message with invalid user payload (action=%s)", action)
        return "skip"
    return await resolve_user_event_workspace_id(
        user,
        AdminDetails(username="webhook-queue", role=AdminRoleData()),
    )


async def resolve_webhook_queue_delivery_scope(
    payload: dict,
    delivery_workspace_id: int | None,
    scope_authorization: str | None = None,
) -> int | None | SkipDelivery:
    """
    Resolve webhook delivery scope for a queued item.

    User-subject payloads always re-derive ownership from the database. A queue
    ``delivery_workspace_id`` or ``scope_authorization='global'`` cannot override
    evidence that the user belongs to a tenant workspace. Explicit global delivery
    requires a verified platform-global user subject or a non-user payload with
    ``scope_authorization='global'``. Unknown or malformed messages fail closed.
    """
    action = payload.get("action")
    is_user_subject = action in USER_SUBJECT_WEBHOOK_ACTIONS

    resolved = await _resolve_user_subject_workspace_from_payload(payload)
    if resolved != "no_user":
        if resolved == "skip":
            return "skip"

        if delivery_workspace_id is not None:
            if resolved != delivery_workspace_id:
                logger.warning(
                    "Webhook queue delivery_workspace_id=%s contradicts user ownership (resolved=%s, action=%s)",
                    delivery_workspace_id,
                    resolved,
                    action,
                )
                return "skip"
            return delivery_workspace_id

        if scope_authorization == "global":
            if resolved is not None:
                logger.warning(
                    "Ignoring global webhook scope for tenant-owned user subject; using workspace_id=%s (action=%s)",
                    resolved,
                    action,
                )
                return resolved
            return None

        return resolved

    if delivery_workspace_id is not None:
        return delivery_workspace_id

    if scope_authorization == "global":
        if is_user_subject:
            logger.warning(
                "Skipping webhook user-subject message without resolvable user id (action=%s)",
                action,
            )
            return "skip"
        return None

    if is_user_subject:
        logger.warning(
            "Skipping webhook user-subject message without resolvable user id (action=%s)",
            action,
        )
        return "skip"

    logger.warning("Skipping unclassified webhook queue message (action=%s)", action)
    return "skip"


def webhook_from_validated_settings(settings: SettingsSchema) -> Webhook | None:
    """Same delivery gate used at enqueue and by the webhook worker."""
    webhook = settings.webhook
    if webhook is None or not webhook.enable or not webhook.webhooks:
        return None
    return webhook


async def webhook_settings_for_delivery(workspace_id: int | None) -> Webhook | None:
    try:
        snapshot = await get_runtime_settings_snapshot(workspace_id)
    except (WorkspaceSettingsNotFoundError, ValueError):
        return None
    return webhook_from_validated_settings(snapshot.settings)


async def webhook_settings_for_delivery_db(
    db: AsyncSession,
    workspace_id: int | None,
) -> Webhook | None:
    """DB-backed delivery settings for scheduler paths (no stale cache reads)."""
    try:
        snapshot = await get_runtime_settings_snapshot(workspace_id, db=db, cache_read=False)
    except (WorkspaceSettingsNotFoundError, ValueError):
        return None
    return webhook_from_validated_settings(snapshot.settings)
