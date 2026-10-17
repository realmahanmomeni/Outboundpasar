"""Phase 2-B.2: workspace-scoped user-event webhook delivery."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.db import GetDB
from app.db.crud.settings import ensure_workspace_settings, get_settings_by_workspace
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User as DbUser
from app.db.models import DataLimitResetStrategy
from app.db.crud.admin import build_admin_details
from app.jobs.send_notifications import send_notifications
from app.models.admin import AdminContactInfo, AdminDetails, AdminRoleData
from app.models.notification_enable import NotificationEnable
from app.models.user import UserNotificationResponse, UserStatus
from app.notification import create_user, modify_user
from app.notification.delivery_scope import webhook_settings_for_delivery
from app.notification.queue_manager import WebhookNotification, get_webhook_queue
from app.notification import webhook as wh
from app.notification.queue_manager import enqueue_webhook as real_enqueue_webhook
from app.services.runtime_settings import clear_runtime_settings_cache
from sqlalchemy import select


async def _tenant(db) -> int:
    t = Tenant(name=f"wh_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"wh_{uuid.uuid4().hex[:10]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


def _webhook_settings(url: str, secret: str) -> dict:
    return {
        "enable": True,
        "webhooks": [{"url": url, "secret": secret}],
        "timeout": 1,
        "recurrent": 3,
        "days_left": [],
        "usage_percent": [],
    }


async def _persist_user(db, admin: Admin) -> UserNotificationResponse:
    row = DbUser(
        username=f"u_{uuid.uuid4().hex[:8]}",
        status=UserStatus.active,
        admin_id=admin.id,
        workspace_id=admin.workspace_id,
    )
    db.add(row)
    await db.flush()
    contact = build_admin_details(admin, include_loaded_metrics=False)
    return UserNotificationResponse(
        id=row.id,
        username=row.username,
        status=UserStatus.active,
        used_traffic=0,
        created_at="2024-01-01T00:00:00Z",
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        admin=contact,
        group_names=[],
    )


@pytest.fixture(autouse=True)
async def _reset_caches():
    await clear_runtime_settings_cache(all_scopes=True)
    yield
    await clear_runtime_settings_cache(all_scopes=True)


async def _configure_workspace_webhook(db, tenant_id: int, admin: Admin, url: str, secret: str) -> None:
    await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin.workspace_id)
    row = await get_settings_by_workspace(db, admin.workspace_id)
    row.webhook = _webhook_settings(url, secret)
    row.notification_enable = NotificationEnable().model_dump()
    row.notification_settings = {
        **(row.notification_settings or {}),
        "notify_telegram": False,
        "max_retries": 3,
    }
    await db.commit()
    await clear_runtime_settings_cache(all_scopes=True)


async def _drain_webhook_queue() -> list[dict]:
    queue = get_webhook_queue()
    drained: list[dict] = []
    while True:
        item = await queue.dequeue(timeout=0.05)
        if not item:
            break
        drained.append(item)
    return drained


@pytest.fixture(autouse=True)
async def _fresh_webhook_queue():
    import app.notification.queue_manager as queue_manager
    from app.notification.nats_queue import InMemoryNotificationQueue

    queue_manager.webhook_queue_instance = InMemoryNotificationQueue()
    yield


async def test_tenant_user_webhook_uses_workspace_configuration():
    url_a = f"https://example.test/hooks/tenant-a/{uuid.uuid4().hex}"
    secret_a = f"secret-a-{uuid.uuid4().hex}"
    workspace_id = None
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        workspace_id = admin.workspace_id
        await _configure_workspace_webhook(db, tenant_id, admin, url_a, secret_a)
        user = await _persist_user(db, admin)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)
        details = AdminDetails(
            username=admin.username,
            id=admin.id,
            workspace_id=admin.workspace_id,
            role=AdminRoleData(),
        )
        captured_workspace_ids: list[int | None] = []
        async def track_enqueue(*args, **kwargs):
            captured_workspace_ids.append(kwargs.get("delivery_workspace_id"))
            return await real_enqueue_webhook(*args, **kwargs)

        with patch("app.notification.webhook.enqueue_webhook", side_effect=track_enqueue):
            with patch("app.notification.claim_notification_slot", return_value=True):
                await create_user(user, details)

    assert captured_workspace_ids == [workspace_id]

    posted_urls = []

    async def capture_post(_client, notifications, webhooks):
        posted_urls.extend(w.url for w in webhooks)
        return True

    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture_post):
        await send_notifications()

    assert posted_urls == [url_a]
    settings = await webhook_settings_for_delivery(workspace_id)
    assert settings is not None
    assert settings.webhooks[0].secret == secret_a


async def test_tenant_a_and_b_webhooks_remain_isolated():
    url_a = f"https://example.test/hooks/a/{uuid.uuid4().hex}"
    url_b = f"https://example.test/hooks/b/{uuid.uuid4().hex}"
    ws_a = None
    ws_b = None
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        ws_a = admin_a.workspace_id
        ws_b = admin_b.workspace_id
        await _configure_workspace_webhook(db, tenant_id, admin_a, url_a, "sec-a")
        await _configure_workspace_webhook(db, tenant_id, admin_b, url_b, "sec-b")
        user_a = await _persist_user(db, admin_a)
        user_b = await _persist_user(db, admin_b)
        details_a = AdminDetails(
            username=admin_a.username,
            id=admin_a.id,
            workspace_id=admin_a.workspace_id,
            role=AdminRoleData(),
        )
        details_b = AdminDetails(
            username=admin_b.username,
            id=admin_b.id,
            workspace_id=admin_b.workspace_id,
            role=AdminRoleData(),
        )
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)
        with patch("app.notification.claim_notification_slot", return_value=True):
            await create_user(user_a, details_a)
            await modify_user(user_b, details_b)

    items = [WebhookNotification(**item) for item in await _drain_webhook_queue()]

    assert {i.delivery_workspace_id for i in items} == {ws_a, ws_b}

    urls_by_workspace: dict[int | None, list[str]] = {}

    async def capture(_client, _notifications, webhooks):
        ws = getattr(capture, "_ws", None)
        urls_by_workspace.setdefault(ws, []).extend(w.url for w in webhooks)
        return True

    for ws in {ws_a, ws_b}:
        capture._ws = ws
        with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
            queue = get_webhook_queue()
            await queue.enqueue(
                WebhookNotification(
                    payload={"action": "user_created", "username": "x"},
                    send_at=0,
                    delivery_workspace_id=ws,
                ).model_dump()
            )
            await send_notifications()

    assert url_a in urls_by_workspace.get(ws_a, [])
    assert url_b in urls_by_workspace.get(ws_b, [])
    assert url_a not in urls_by_workspace.get(ws_b, [])
    assert url_b not in urls_by_workspace.get(ws_a, [])


async def test_unresolved_user_does_not_enqueue_workspace_webhook():
    async with GetDB() as db:
        row = DbUser(
            username=f"u_{uuid.uuid4().hex[:8]}",
            status=UserStatus.active,
            admin_id=None,
            workspace_id=None,
        )
        db.add(row)
        await db.flush()
        user = UserNotificationResponse(
            id=row.id,
            username=row.username,
            status=UserStatus.active,
            used_traffic=0,
            created_at="2024-01-01T00:00:00Z",
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            admin=None,
            group_names=[],
        )
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)
        with patch("app.notification.claim_notification_slot", return_value=True):
            await create_user(user, _owner_actor())

    assert await _drain_webhook_queue() == []


def _owner_actor() -> AdminDetails:
    return AdminDetails(
        username="platform_owner",
        role=AdminRoleData(name="owner", is_owner=True),
    )


async def test_missing_workspace_settings_does_not_fall_back_to_global_webhook():
    global_url = f"https://example.test/global/{uuid.uuid4().hex}"
    async with GetDB() as db:
        from app.db.crud.settings import get_global_settings

        global_row = await get_global_settings(db)
        assert global_row is not None
        global_row.webhook = _webhook_settings(global_url, "global-secret")
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = await _persist_user(db, admin)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

        await wh.notify(
            wh.UserCreated(username=user.username, user=user, by=_owner_actor()),
            delivery_workspace_id=admin.workspace_id,
        )

    posted = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == []


async def test_retry_preserves_delivery_workspace_id():
    ws = 4242
    payload = {"action": "user_updated", "username": "retry-user"}
    with patch("app.jobs.send_notifications.send_to_all_webhooks", new_callable=AsyncMock, return_value=False):
        with patch(
            "app.jobs.send_notifications.webhook_settings_for_delivery",
            new_callable=AsyncMock,
        ) as load_settings:
            from app.models.settings import Webhook, WebhookInfo

            load_settings.return_value = Webhook(
                enable=True,
                webhooks=[WebhookInfo(url="https://example.test/retry", secret="s")],
                timeout=1,
                recurrent=3,
                days_left=[],
                usage_percent=[],
            )
            queue = get_webhook_queue()
            await queue.enqueue(
                WebhookNotification(
                    payload=payload,
                    send_at=0,
                    delivery_workspace_id=ws,
                    tries=0,
                ).model_dump()
            )
            await send_notifications()

    requeued = await queue.dequeue(timeout=0.01)
    assert requeued is not None
    parsed = WebhookNotification(**requeued)
    assert parsed.delivery_workspace_id == ws
    assert parsed.tries == 1
    assert parsed.scope_authorization is None


async def test_legacy_queue_item_without_scope_uses_tenant_webhook_not_global():
    global_url = f"https://example.test/global-legacy/{uuid.uuid4().hex}"
    tenant_url = f"https://example.test/tenant-legacy/{uuid.uuid4().hex}"
    workspace_id = None
    async with GetDB() as db:
        from app.db.crud.settings import get_global_settings

        global_row = await get_global_settings(db)
        assert global_row is not None
        global_row.webhook = _webhook_settings(global_url, "global-secret")
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        workspace_id = admin.workspace_id
        await _configure_workspace_webhook(db, tenant_id, admin, tenant_url, "tenant-secret")
        user = await _persist_user(db, admin)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)
        payload = wh.UserCreated(
            username=user.username,
            user=user,
            by=AdminDetails(username=admin.username, id=admin.id, workspace_id=workspace_id, role=AdminRoleData()),
        )
        from fastapi.encoders import jsonable_encoder

        legacy_payload = jsonable_encoder(payload)

    posted = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue(
        {
            "type": "webhook",
            "payload": legacy_payload,
            "send_at": 0,
            "tries": 0,
        }
    )
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == [tenant_url]
    assert global_url not in posted


async def test_legacy_queue_item_orphan_user_does_not_use_global_webhook():
    global_url = f"https://example.test/global-orphan/{uuid.uuid4().hex}"
    async with GetDB() as db:
        from app.db.crud.settings import get_global_settings

        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_settings(global_url, "global-secret")
        row = DbUser(
            username=f"u_{uuid.uuid4().hex[:8]}",
            status=UserStatus.active,
            admin_id=None,
            workspace_id=None,
        )
        db.add(row)
        await db.flush()
        user = UserNotificationResponse(
            id=row.id,
            username=row.username,
            status=UserStatus.active,
            used_traffic=0,
            created_at="2024-01-01T00:00:00Z",
            data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
            admin=None,
            group_names=[],
        )
        await db.commit()
        from fastapi.encoders import jsonable_encoder

        legacy_payload = jsonable_encoder(
            wh.UserEnabled(username=user.username, user=user),
        )

    posted = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue({"type": "webhook", "payload": legacy_payload, "send_at": 0, "tries": 0})
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == []


async def test_unclassified_message_without_user_does_not_use_global_webhook():
    global_url = f"https://example.test/unclassified/{uuid.uuid4().hex}"
    async with GetDB() as db:
        from app.db.crud.settings import get_global_settings

        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_settings(global_url, "global-secret")
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    posted = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue(
        {
            "type": "webhook",
            "payload": {"action": "unknown_event_type", "username": "ghost"},
            "send_at": 0,
            "tries": 0,
        }
    )
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == []


async def test_explicit_global_authorization_uses_global_settings_without_user_subject():
    global_url = f"https://example.test/explicit-global/{uuid.uuid4().hex}"
    async with GetDB() as db:
        from app.db.crud.settings import get_global_settings

        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_settings(global_url, "global-secret")
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

    posted = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue(
        WebhookNotification(
            payload={"action": "infrastructure_ping", "detail": "ok"},
            send_at=0,
            delivery_workspace_id=None,
            scope_authorization="global",
            tries=0,
        ).model_dump()
    )
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == [global_url]


async def test_user_subject_action_without_user_id_fails_closed():
    global_url = f"https://example.test/missing-user/{uuid.uuid4().hex}"
    async with GetDB() as db:
        from app.db.crud.settings import get_global_settings

        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_settings(global_url, "global-secret")
        await db.commit()

    posted = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue(
        {
            "type": "webhook",
            "payload": {"action": "user_created", "username": "x"},
            "send_at": 0,
            "tries": 0,
        }
    )
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == []


def test_webhook_notification_serialization_preserves_scope_authorization():
    item = WebhookNotification(
        payload={"action": "user_created"},
        send_at=1.0,
        delivery_workspace_id=99,
        scope_authorization=None,
        tries=2,
    ).model_dump()
    parsed = WebhookNotification(**item)
    assert parsed.delivery_workspace_id == 99
    assert parsed.scope_authorization is None
    assert parsed.tries == 2

    global_item = WebhookNotification(
        payload={"action": "x"},
        send_at=1.0,
        delivery_workspace_id=None,
        scope_authorization="global",
        tries=0,
    ).model_dump()
    assert WebhookNotification(**global_item).scope_authorization == "global"


async def test_notify_does_not_infer_global_scope_from_none_workspace_id():
    url_t = f"https://example.test/producer-none/{uuid.uuid4().hex}"
    workspace_id = None
    captured: list[dict] = []

    async def track_enqueue(*args, **kwargs):
        captured.append(kwargs)
        return await real_enqueue_webhook(*args, **kwargs)

    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        workspace_id = admin.workspace_id
        await _configure_workspace_webhook(db, tenant_id, admin, url_t, "sec")
        user = await _persist_user(db, admin)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)
        with patch("app.notification.webhook.enqueue_webhook", side_effect=track_enqueue):
            await wh.notify(
                wh.UserCreated(
                    username=user.username,
                    user=user,
                    by=AdminDetails(username=admin.username, id=admin.id, workspace_id=workspace_id, role=AdminRoleData()),
                ),
                delivery_workspace_id=None,
            )

    assert len(captured) == 1
    assert captured[0]["delivery_workspace_id"] is None
    assert captured[0]["scope_authorization"] is None


async def test_notify_strips_global_scope_authorization_for_user_subject_payloads():
    captured: list[dict] = []

    async def track_enqueue(*args, **kwargs):
        captured.append(kwargs)
        return await real_enqueue_webhook(*args, **kwargs)

    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await _configure_workspace_webhook(db, tenant_id, admin, "https://example.test/x", "s")
        user = await _persist_user(db, admin)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)
        with patch("app.notification.webhook.enqueue_webhook", side_effect=track_enqueue):
            await wh.notify(
                wh.UserCreated(
                    username=user.username,
                    user=user,
                    by=AdminDetails(username=admin.username, id=admin.id, workspace_id=admin.workspace_id, role=AdminRoleData()),
                ),
                delivery_workspace_id=None,
                scope_authorization="global",
            )

    assert captured[0]["scope_authorization"] is None


async def test_forged_global_scope_on_tenant_user_delivers_to_tenant_not_global():
    global_url = f"https://example.test/forged-global/{uuid.uuid4().hex}"
    tenant_url = f"https://example.test/forged-tenant/{uuid.uuid4().hex}"
    workspace_id = None
    async with GetDB() as db:
        from app.db.crud.settings import get_global_settings

        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_settings(global_url, "global-secret")
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        workspace_id = admin.workspace_id
        await _configure_workspace_webhook(db, tenant_id, admin, tenant_url, "tenant-secret")
        user = await _persist_user(db, admin)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)
        from fastapi.encoders import jsonable_encoder

        payload = jsonable_encoder(
            wh.UserCreated(
                username=user.username,
                user=user,
                by=AdminDetails(username=admin.username, id=admin.id, workspace_id=workspace_id, role=AdminRoleData()),
            )
        )

    posted = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue(
        WebhookNotification(
            payload=payload,
            send_at=0,
            delivery_workspace_id=None,
            scope_authorization="global",
            tries=0,
        ).model_dump()
    )
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == [tenant_url]
    assert global_url not in posted


async def test_forged_delivery_workspace_id_contradicting_user_ownership_skips():
    url_a = f"https://example.test/ws-a/{uuid.uuid4().hex}"
    url_b = f"https://example.test/ws-b/{uuid.uuid4().hex}"
    ws_a = ws_b = None
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        ws_a, ws_b = admin_a.workspace_id, admin_b.workspace_id
        await _configure_workspace_webhook(db, tenant_id, admin_a, url_a, "sec-a")
        await _configure_workspace_webhook(db, tenant_id, admin_b, url_b, "sec-b")
        user_a = await _persist_user(db, admin_a)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)
        from fastapi.encoders import jsonable_encoder

        payload = jsonable_encoder(
            wh.UserUpdated(
                username=user_a.username,
                user=user_a,
                by=AdminDetails(username=admin_a.username, id=admin_a.id, workspace_id=ws_a, role=AdminRoleData()),
            )
        )

    posted = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue(
        WebhookNotification(
            payload=payload,
            send_at=0,
            delivery_workspace_id=ws_b,
            scope_authorization=None,
            tries=0,
        ).model_dump()
    )
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == []


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


async def test_webhook_delivery_uses_refreshed_workspace_url_not_stale_cache():
    url_v1 = f"https://example.test/cache-v1/{uuid.uuid4().hex}"
    url_v2 = f"https://example.test/cache-v2/{uuid.uuid4().hex}"
    workspace_id = None
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        workspace_id = admin.workspace_id
        await _configure_workspace_webhook(db, tenant_id, admin, url_v1, "sec")
        user = await _persist_user(db, admin)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)
        await webhook_settings_for_delivery(workspace_id)
        row = await get_settings_by_workspace(db, admin.workspace_id)
        row.webhook = _webhook_settings(url_v2, "sec2")
        await db.commit()
        await clear_runtime_settings_cache(workspace_id=workspace_id)
        from fastapi.encoders import jsonable_encoder

        payload = jsonable_encoder(
            wh.UserUpdated(
                username=user.username,
                user=user,
                by=AdminDetails(username=admin.username, id=admin.id, workspace_id=workspace_id, role=AdminRoleData()),
            )
        )

    posted: list[str] = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue(
        WebhookNotification(
            payload=payload,
            send_at=0,
            delivery_workspace_id=workspace_id,
            tries=0,
        ).model_dump()
    )
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == [url_v2]
    assert url_v1 not in posted


async def test_global_owner_user_with_explicit_global_scope_uses_global_webhook():
    global_url = f"https://example.test/global-owner-queue/{uuid.uuid4().hex}"
    async with GetDB() as db:
        from app.db.crud.settings import get_global_settings

        global_row = await get_global_settings(db)
        global_row.webhook = _webhook_settings(global_url, "global-secret")
        owner = await _owner_admin(db)
        user = await _persist_user(db, owner)
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)
        from fastapi.encoders import jsonable_encoder

        payload = jsonable_encoder(wh.UserEnabled(username=user.username, user=user))

    posted = []

    async def capture(_c, _n, webhooks):
        posted.extend(w.url for w in webhooks)
        return True

    queue = get_webhook_queue()
    await queue.enqueue(
        WebhookNotification(
            payload=payload,
            send_at=0,
            delivery_workspace_id=None,
            scope_authorization="global",
            tries=0,
        ).model_dump()
    )
    with patch("app.jobs.send_notifications.send_to_all_webhooks", side_effect=capture):
        await send_notifications()

    assert posted == [global_url]
