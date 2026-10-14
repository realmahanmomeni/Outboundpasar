"""Phase 2-B.3: workspace-scoped scheduled reminder webhooks."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime as dt, timedelta as td

import pytest

from app.db import GetDB
from app.db.crud.settings import ensure_workspace_settings, get_global_settings, get_settings_by_workspace
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, DataLimitResetStrategy, Tenant, TenantStatus, User as DbUser
from app.jobs.review_users import days_left_notification_job, usage_percent_notification_job
from app.jobs.send_notifications import send_notifications
from app.models.notification_enable import NotificationEnable
from app.models.user import UserStatus
from app.notification.queue_manager import WebhookNotification, get_webhook_queue
from app.services.runtime_settings import clear_runtime_settings_cache
from sqlalchemy import select
from unittest.mock import patch


async def _tenant(db) -> int:
    t = Tenant(name=f"sch_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"sch_{uuid.uuid4().hex[:10]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


def _webhook_payload(
    url: str,
    secret: str,
    *,
    enable: bool = True,
    usage_percent: list[int] | None = None,
    days_left: list[int] | None = None,
    webhooks: list[dict] | None = None,
) -> dict:
    return {
        "enable": enable,
        "webhooks": webhooks if webhooks is not None else [{"url": url, "secret": secret}],
        "timeout": 1,
        "recurrent": 3,
        "days_left": days_left if days_left is not None else [],
        "usage_percent": usage_percent if usage_percent is not None else [],
    }


async def _set_workspace_webhook(db, tenant_id: int, admin: Admin, payload: dict) -> None:
    await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin.workspace_id)
    row = await get_settings_by_workspace(db, admin.workspace_id)
    row.webhook = payload
    row.notification_enable = NotificationEnable().model_dump()
    row.notification_settings = {**(row.notification_settings or {}), "notify_telegram": False, "max_retries": 3}


async def _user_at_usage(db, admin: Admin, percent: int = 80) -> DbUser:
    data_limit = 10_000
    used = int(data_limit * percent / 100)
    row = DbUser(
        username=f"u_{uuid.uuid4().hex[:8]}",
        status=UserStatus.active,
        admin_id=admin.id,
        workspace_id=admin.workspace_id,
        data_limit=data_limit,
        used_traffic=used,
    )
    db.add(row)
    await db.flush()
    return row


async def _user_days_left(db, admin: Admin, days: int = 7) -> DbUser:
    row = DbUser(
        username=f"u_{uuid.uuid4().hex[:8]}",
        status=UserStatus.active,
        admin_id=admin.id,
        workspace_id=admin.workspace_id,
    )
    # SQL ``days_left`` uses floor(julianday); add slack so the threshold still matches.
    row.expire = dt.now(UTC) + td(days=days, hours=2)
    db.add(row)
    await db.flush()
    return row


async def _drain_webhook_queue() -> list[WebhookNotification]:
    queue = get_webhook_queue()
    drained: list[WebhookNotification] = []
    while True:
        item = await queue.dequeue(timeout=0.05)
        if not item:
            break
        drained.append(WebhookNotification(**item))
    return drained


@pytest.fixture(autouse=True)
async def _reset_caches():
    await clear_runtime_settings_cache(all_scopes=True)
    yield
    await clear_runtime_settings_cache(all_scopes=True)


@pytest.fixture(autouse=True)
async def _fresh_webhook_queue():
    import app.notification.queue_manager as queue_manager
    from app.notification.nats_queue import InMemoryNotificationQueue

    queue_manager.webhook_queue_instance = InMemoryNotificationQueue()
    yield


async def test_usage_reminder_tenant_a_enabled_b_disabled_only_a_enqueues():
    url_a = f"https://example.test/sch/a/{uuid.uuid4().hex}"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await _set_workspace_webhook(
            db,
            tenant_id,
            admin_a,
            _webhook_payload(url_a, "sec-a", usage_percent=[80]),
        )
        await _set_workspace_webhook(
            db,
            tenant_id,
            admin_b,
            _webhook_payload("https://example.test/disabled", "x", enable=False, usage_percent=[80]),
        )
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload("https://example.test/global-off", "g", enable=False, usage_percent=[80])
        await _user_at_usage(db, admin_a, 80)
        await _user_at_usage(db, admin_b, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    items = await _drain_webhook_queue()
    assert len(items) == 1
    assert items[0].delivery_workspace_id == admin_a.workspace_id
    assert items[0].payload["action"] == "reached_usage_percent"


async def test_usage_reminder_global_disabled_tenant_enabled_still_enqueues():
    url_t = f"https://example.test/sch/tenant-on/{uuid.uuid4().hex}"
    ws = None
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        ws = admin.workspace_id
        await _set_workspace_webhook(db, tenant_id, admin, _webhook_payload(url_t, "sec", usage_percent=[80]))
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload("https://example.test/global-off", "g", enable=False, usage_percent=[80])
        await _user_at_usage(db, admin, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    items = await _drain_webhook_queue()
    assert len(items) == 1
    assert items[0].delivery_workspace_id == ws


async def test_usage_reminder_global_enabled_tenant_disabled_does_not_enqueue():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await _set_workspace_webhook(
            db,
            tenant_id,
            admin,
            _webhook_payload("https://example.test/off", "x", enable=False, usage_percent=[80]),
        )
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload(
            f"https://example.test/global/{uuid.uuid4().hex}",
            "g",
            enable=True,
            usage_percent=[80],
        )
        await _user_at_usage(db, admin, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    assert await _drain_webhook_queue() == []


async def test_usage_reminder_two_tenants_use_distinct_workspace_scope():
    url_a = f"https://example.test/sch/ws-a/{uuid.uuid4().hex}"
    url_b = f"https://example.test/sch/ws-b/{uuid.uuid4().hex}"
    ws_a = ws_b = None
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        ws_a, ws_b = admin_a.workspace_id, admin_b.workspace_id
        await _set_workspace_webhook(db, tenant_id, admin_a, _webhook_payload(url_a, "a", usage_percent=[80]))
        await _set_workspace_webhook(db, tenant_id, admin_b, _webhook_payload(url_b, "b", usage_percent=[80]))
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload("https://example.test/g", "g", enable=False, usage_percent=[80])
        await _user_at_usage(db, admin_a, 80)
        await _user_at_usage(db, admin_b, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    items = await _drain_webhook_queue()
    assert len(items) == 2
    by_ws = {i.delivery_workspace_id: i for i in items}
    assert set(by_ws) == {ws_a, ws_b}

    posted: dict[int | None, list[str]] = {}

    async def capture(_c, _n, webhooks):
        ws = getattr(capture, "_ws", None)
        posted.setdefault(ws, []).extend(w.url for w in webhooks)
        return True

    for ws, expected_url in ((ws_a, url_a), (ws_b, url_b)):
        capture._ws = ws
        subset = [i for i in items if i.delivery_workspace_id == ws]
        queue = get_webhook_queue()
        for item in subset:
            await queue.enqueue(item.model_dump())
        with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
            await send_notifications()

    assert url_a in posted.get(ws_a, [])
    assert url_b in posted.get(ws_b, [])
    assert url_a not in posted.get(ws_b, [])
    assert url_b not in posted.get(ws_a, [])


async def test_usage_reminder_incomplete_workspace_webhook_config_skips():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await _set_workspace_webhook(
            db,
            tenant_id,
            admin,
            _webhook_payload("https://example.test/x", "s", usage_percent=[80], webhooks=[]),
        )
        await _user_at_usage(db, admin, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    assert await _drain_webhook_queue() == []


async def test_usage_reminder_missing_workspace_settings_does_not_fall_back_to_global():
    global_url = f"https://example.test/sch/global-fallback/{uuid.uuid4().hex}"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await _set_workspace_webhook(
            db,
            tenant_id,
            admin,
            _webhook_payload(f"https://example.test/tenant/{uuid.uuid4().hex}", "s", usage_percent=[80]),
        )
        settings_row = await get_settings_by_workspace(db, admin.workspace_id)
        await db.delete(settings_row)
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload(global_url, "g", enable=True, usage_percent=[80])
        await _user_at_usage(db, admin, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    assert await _drain_webhook_queue() == []


async def test_usage_reminder_orphan_user_does_not_enqueue():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await _set_workspace_webhook(
            db,
            tenant_id,
            admin,
            _webhook_payload(f"https://example.test/{uuid.uuid4().hex}", "s", usage_percent=[80]),
        )
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload(
            f"https://example.test/global/{uuid.uuid4().hex}",
            "g",
            enable=True,
            usage_percent=[80],
        )
        row = DbUser(
            username=f"orphan_{uuid.uuid4().hex[:8]}",
            status=UserStatus.active,
            admin_id=None,
            workspace_id=None,
            data_limit=10_000,
            used_traffic=8_000,
        )
        db.add(row)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    assert await _drain_webhook_queue() == []


async def test_days_left_reminder_respects_workspace_webhook_enablement():
    url_t = f"https://example.test/sch/days/{uuid.uuid4().hex}"
    ws = None
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        ws = admin.workspace_id
        await _set_workspace_webhook(db, tenant_id, admin, _webhook_payload(url_t, "s", days_left=[7]))
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload("https://example.test/g", "g", enable=False, days_left=[7])
        await _user_days_left(db, admin, 7)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await days_left_notification_job()
    items = await _drain_webhook_queue()
    assert len(items) == 1
    assert items[0].delivery_workspace_id == ws
    assert items[0].payload["action"] == "reached_days_left"


async def _owner_admin(db) -> Admin:
    owner_role = (await db.execute(select(AdminRole).where(AdminRole.is_owner.is_(True)))).scalar_one()
    row = Admin(
        username=f"own_{uuid.uuid4().hex[:8]}",
        hashed_password="x",
        role_id=owner_role.id,
        tenant_id=None,
        workspace_id=None,
    )
    db.add(row)
    await db.flush()
    return row


async def _global_user_at_usage(db, owner_admin: Admin, percent: int = 80) -> DbUser:
    data_limit = 10_000
    used = int(data_limit * percent / 100)
    row = DbUser(
        username=f"gu_{uuid.uuid4().hex[:8]}",
        status=UserStatus.active,
        admin_id=owner_admin.id,
        workspace_id=None,
        data_limit=data_limit,
        used_traffic=used,
    )
    db.add(row)
    await db.flush()
    return row


async def test_global_owner_usage_reminder_enqueues_to_global_webhook():
    global_url = f"https://example.test/sch/global-owner/{uuid.uuid4().hex}"
    async with GetDB() as db:
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload(global_url, "global-secret", usage_percent=[80])
        owner = await _owner_admin(db)
        await _global_user_at_usage(db, owner, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    items = await _drain_webhook_queue()
    assert len(items) == 1
    assert items[0].delivery_workspace_id is None
    assert items[0].scope_authorization is None
    assert items[0].payload["action"] == "reached_usage_percent"

    posted: list[str] = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue(items[0].model_dump())
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == [global_url]


async def test_non_owner_global_user_does_not_use_global_webhook():
    global_url = f"https://example.test/sch/non-owner/{uuid.uuid4().hex}"
    async with GetDB() as db:
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload(global_url, "g", usage_percent=[80])
        operator_role = (await db.execute(select(AdminRole).where(AdminRole.name == "operator"))).scalar_one()
        admin = Admin(
            username=f"op_{uuid.uuid4().hex[:8]}",
            hashed_password="x",
            role_id=operator_role.id,
            tenant_id=None,
            workspace_id=None,
        )
        db.add(admin)
        await db.flush()
        await _global_user_at_usage(db, admin, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    assert await _drain_webhook_queue() == []


async def test_per_workspace_threshold_isolation_not_global():
    url_b = f"https://example.test/sch/thr-b/{uuid.uuid4().hex}"
    ws_b = None
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        ws_b = admin_b.workspace_id
        await _set_workspace_webhook(
            db,
            tenant_id,
            admin_a,
            _webhook_payload("https://example.test/a-only-90", "a", usage_percent=[90]),
        )
        await _set_workspace_webhook(db, tenant_id, admin_b, _webhook_payload(url_b, "b", usage_percent=[80]))
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload("https://example.test/global-80", "g", usage_percent=[80])
        await _user_at_usage(db, admin_a, 85)
        await _user_at_usage(db, admin_b, 85)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    items = await _drain_webhook_queue()
    assert len(items) == 1
    assert items[0].delivery_workspace_id == ws_b

    posted: list[str] = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue(items[0].model_dump())
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()
    assert posted == [url_b]


async def test_tenant_empty_thresholds_do_not_inherit_global_at_schedule_time():
    global_url = f"https://example.test/sch/no-inherit/{uuid.uuid4().hex}"
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await _set_workspace_webhook(
            db,
            tenant_id,
            admin,
            _webhook_payload(f"https://example.test/tenant/{uuid.uuid4().hex}", "t", usage_percent=[]),
        )
        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_payload(global_url, "g", usage_percent=[80])
        await _user_at_usage(db, admin, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    assert await _drain_webhook_queue() == []


async def test_malformed_workspace_webhook_does_not_crash_scheduler_or_enqueue():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await _set_workspace_webhook(
            db,
            tenant_id,
            admin,
            _webhook_payload(f"https://example.test/ok/{uuid.uuid4().hex}", "s", usage_percent=[80]),
        )
        row = await get_settings_by_workspace(db, admin.workspace_id)
        row.webhook = {
            "enable": True,
            "webhooks": [{"url": "https://example.test/bad", "secret": "x"}],
            "timeout": 0,
            "recurrent": 3,
            "usage_percent": [80],
        }
        await _user_at_usage(db, admin, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    assert await _drain_webhook_queue() == []


async def test_scheduler_reminder_queue_items_carry_scope_for_retry():
    url = f"https://example.test/sch/retry/{uuid.uuid4().hex}"
    ws = None
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        ws = admin.workspace_id
        await _set_workspace_webhook(db, tenant_id, admin, _webhook_payload(url, "s", usage_percent=[80]))
        await _user_at_usage(db, admin, 80)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    await usage_percent_notification_job()
    items = await _drain_webhook_queue()
    assert len(items) == 1
    item = items[0]
    assert item.delivery_workspace_id == ws
    assert item.scope_authorization is None

    queue = get_webhook_queue()
    await queue.enqueue(item.model_dump())
    with patch("app.jobs.send_notifications.send_to_all_webhooks", return_value=False):
        await send_notifications()
    requeued = await queue.dequeue(timeout=0.05)
    assert requeued is not None
    parsed = WebhookNotification(**requeued)
    assert parsed.delivery_workspace_id == ws
    assert parsed.payload["action"] == "reached_usage_percent"
