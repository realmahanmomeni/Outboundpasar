"""Workspace-scoped scheduled user reminder webhooks."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.user import (
    bulk_create_notification_reminders,
    get_days_left_reached_users,
    get_usage_percentage_reached_users,
)
from app.db.models import ReminderType, Settings
from app.jobs.dependencies import SYSTEM_ADMIN
from app.models.settings import Webhook
from app.models.user import UserNotificationResponse
from app.notification import webhook as wh
from app.notification.delivery_scope import (
    resolve_user_event_workspace_id,
    webhook_from_validated_settings,
    webhook_settings_for_delivery_db,
)
from app.services.runtime_settings import WorkspaceSettingsNotFoundError, get_runtime_settings_snapshot
from app.utils.logger import get_logger

logger = get_logger("webhook-reminders")


async def _enabled_webhook_scopes(db: AsyncSession) -> list[tuple[int | None, Webhook]]:
    """Scopes with validated, deliverable webhook settings (canonical runtime schema)."""
    rows = (await db.execute(select(Settings))).scalars().all()
    scopes: list[tuple[int | None, Webhook]] = []
    for row in rows:
        workspace_id = row.workspace_id
        try:
            snapshot = await get_runtime_settings_snapshot(workspace_id, db=db, cache_read=False)
        except (WorkspaceSettingsNotFoundError, ValueError):
            logger.warning(
                "Skipping settings row without valid runtime snapshot (workspace_id=%s)",
                workspace_id,
            )
            continue
        webhook = webhook_from_validated_settings(snapshot.settings)
        if webhook is None:
            continue
        scopes.append((workspace_id, webhook))
    return scopes


async def _resolve_reminder_delivery_workspace(
    db: AsyncSession,
    user_model: UserNotificationResponse,
    scope_workspace_id: int | None,
) -> int | None | str:
    resolved = await resolve_user_event_workspace_id(user_model, SYSTEM_ADMIN)
    if resolved == "skip":
        return "skip"
    if resolved != scope_workspace_id:
        return "skip"
    if await webhook_settings_for_delivery_db(db, resolved) is None:
        return "skip"
    return resolved


async def process_usage_percent_reminders(db: AsyncSession) -> None:
    for scope_workspace_id, webhook in await _enabled_webhook_scopes(db):
        for percent in webhook.usage_percent:
            users = await get_usage_percentage_reached_users(db, percent, workspace_id=scope_workspace_id)
            webhook_data: list[wh.Notification] = []
            reminder_data: list[dict] = []

            for user in users:
                user_model = UserNotificationResponse.model_validate(user)
                delivery_workspace_id = await _resolve_reminder_delivery_workspace(
                    db, user_model, scope_workspace_id
                )
                if delivery_workspace_id == "skip":
                    continue

                usage_percentage = user.usage_percentage
                webhook_data.append(
                    wh.ReachedUsagePercent(
                        username=user_model.username,
                        user=user_model,
                        used_percent=usage_percentage,
                    )
                )
                reminder_data.append(
                    {
                        "user_id": user.id,
                        "type": ReminderType.data_usage,
                        "threshold": percent,
                        "expires_at": user.expire if user.expire else None,
                    }
                )

            if reminder_data:
                await bulk_create_notification_reminders(db, reminder_data)
            if webhook_data:
                await wh.bulk_notify(webhook_data, delivery_workspace_id=scope_workspace_id)


async def process_days_left_reminders(db: AsyncSession) -> None:
    for scope_workspace_id, webhook in await _enabled_webhook_scopes(db):
        for days in webhook.days_left:
            users = await get_days_left_reached_users(db, days, workspace_id=scope_workspace_id)
            webhook_data: list[wh.Notification] = []
            reminder_data: list[dict] = []

            for user in users:
                user_model = UserNotificationResponse.model_validate(user)
                delivery_workspace_id = await _resolve_reminder_delivery_workspace(
                    db, user_model, scope_workspace_id
                )
                if delivery_workspace_id == "skip":
                    continue

                days_left = user.days_left
                webhook_data.append(
                    wh.ReachedDaysLeft(
                        username=user_model.username,
                        user=user_model,
                        days_left=days_left,
                    )
                )
                reminder_data.append(
                    {
                        "user_id": user.id,
                        "type": ReminderType.expiration_date,
                        "threshold": days,
                        "expires_at": user.expire,
                    }
                )

            if reminder_data:
                await bulk_create_notification_reminders(db, reminder_data)
            if webhook_data:
                await wh.bulk_notify(webhook_data, delivery_workspace_id=scope_workspace_id)
