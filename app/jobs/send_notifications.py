import asyncio
from collections import defaultdict
from datetime import UTC, datetime as dt, timedelta as td

import aiohttp
from sqlalchemy import delete

from app import on_shutdown, scheduler
from app.db import GetDB
from app.db.models import NotificationReminder
from app.models.settings import Webhook
from app.notification.delivery_scope import resolve_webhook_queue_delivery_scope, webhook_settings_for_delivery
from app.notification.queue_manager import (
    WebhookNotification,
    enqueue_webhook,
    get_webhook_queue,
    shutdown_webhook_queue,
)
from app.utils.http_client import create_outbound_http_session
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("send-notification")


async def send_to_all_webhooks(client: aiohttp.ClientSession, notifications, webhooks):
    """
    Send the notifications to all webhooks concurrently.
    Returns True if at least one webhook succeeds.
    notifications: list of already JSON-serializable dicts (webhook payloads)
    """
    if not notifications:
        return True

    payload = notifications  # Already JSON-serializable, no need for jsonable_encoder

    async def send_one(webhook):
        webhook_headers = {"x-webhook-secret": webhook.secret} if webhook.secret else None
        try:
            r = await client.post(webhook.url, json=payload, headers=webhook_headers)
            if r.status in (200, 201, 202, 204):
                return True
            else:
                logger.error(f"Webhook {webhook.url} failed: {r.status} - {await r.text()}")
        except Exception as err:
            logger.error(f"Webhook {webhook.url} exception: {err}")
        return False

    results = await asyncio.gather(*(send_one(webhook) for webhook in webhooks))
    return any(results)


async def send_notifications():
    """Drain queued webhooks through a client-scoped trusted TLS context."""
    logger.debug("Processing notifications batch")

    processed = 0
    failed_to_requeue = []
    ready_notifications: list[WebhookNotification] = []
    current_time = dt.now(UTC).timestamp()

    try:
        webhook_queue = get_webhook_queue()
        while True:
            try:
                item = await webhook_queue.dequeue(timeout=1)
            except Exception:
                break

            if not item:
                break

            try:
                notification = WebhookNotification(**item)
            except Exception:
                continue

            delivery_scope = await resolve_webhook_queue_delivery_scope(
                notification.payload,
                notification.delivery_workspace_id,
                notification.scope_authorization,
            )
            if delivery_scope == "skip":
                continue

            if delivery_scope is None:
                scope_updates = {
                    "delivery_workspace_id": None,
                    "scope_authorization": "global",
                }
            else:
                scope_updates = {
                    "delivery_workspace_id": delivery_scope,
                    "scope_authorization": None,
                }
            notification = notification.model_copy(update=scope_updates)

            settings = await webhook_settings_for_delivery(notification.delivery_workspace_id)
            if settings is None or not settings.enable:
                continue

            if notification.tries >= settings.recurrent:
                continue

            if notification.send_at > current_time:
                failed_to_requeue.append(notification)
                continue

            ready_notifications.append(notification)

        grouped: dict[int | None, list[WebhookNotification]] = defaultdict(list)
        for notification in ready_notifications:
            grouped[notification.delivery_workspace_id].append(notification)

        for workspace_id, group in grouped.items():
            settings = await webhook_settings_for_delivery(workspace_id)
            if settings is None or not settings.enable:
                continue

            async with create_outbound_http_session(
                proxy=settings.proxy_url if settings.proxy_url else None
            ) as client:
                batch_size = 50
                for start in range(0, len(group), batch_size):
                    batch = group[start : start + batch_size]
                    logger.info(
                        f"Sending batch of {len(batch)} notifications to {len(settings.webhooks)} webhooks "
                        f"(workspace_id={workspace_id}, chunk {start // batch_size + 1})"
                    )
                    payloads = [notif.payload for notif in batch]
                    success = await send_to_all_webhooks(client, payloads, settings.webhooks)

                    if not success:
                        retry_at = dt.now(UTC).timestamp()
                        for notification in batch:
                            notification.tries += 1
                            if notification.tries < settings.recurrent:
                                notification.send_at = retry_at + settings.timeout
                                failed_to_requeue.append(notification)

                    processed += len(batch)

    finally:
        for notif in failed_to_requeue:
            await enqueue_webhook(
                notif.payload,
                send_at=notif.send_at,
                tries=notif.tries,
                delivery_workspace_id=notif.delivery_workspace_id,
                scope_authorization=notif.scope_authorization,
            )

        if processed or failed_to_requeue:
            logger.info(f"Processed {processed} notifications, requeued {len(failed_to_requeue)}")


async def delete_expired_reminders() -> None:
    async with GetDB() as db:
        # Get current UTC time and convert to naive datetime
        now_utc = dt.now(tz=UTC)
        now_naive = now_utc.replace(tzinfo=None)

        result = await db.execute(delete(NotificationReminder).where(NotificationReminder.expires_at < now_naive))
        logger.info(f"Cleaned up {result.rowcount} expired reminders")


async def send_pending_notifications_before_shutdown():
    logger.info("Webhook final flush before shutdown")
    await send_notifications()


if runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        send_notifications,
        "interval",
        seconds=job_settings.send_notifications_interval,
        max_instances=1,
        coalesce=True,
        id="send_notifications",
        replace_existing=True,
    )
    scheduler.add_job(
        delete_expired_reminders,
        "interval",
        hours=6,
        start_date=dt.now(UTC) + td(minutes=5),
        id="delete_expired_notification_reminders",
        replace_existing=True,
    )
    on_shutdown(send_pending_notifications_before_shutdown)
    on_shutdown(shutdown_webhook_queue)  # Must run after flush to keep queue alive
