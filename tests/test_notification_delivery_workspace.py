"""Phase 2-B.1: workspace-scoped event-driven Telegram/Discord delivery."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.db import GetDB
from app.db.crud.settings import ensure_workspace_settings, get_settings_by_workspace
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User as DbUser
from app.db.models import DataLimitResetStrategy
from app.db.crud.admin import build_admin_details
from app.models.admin import AdminContactInfo, AdminDetails, AdminRoleData
from app.models.user import UserNotificationResponse, UserStatus
from app.notification import create_user
from app.notification.client import process_notification
from app.notification.delivery_scope import (
    _UserOwnerScope,
    _lookup_user_owner_scope,
    _resolve_user_owner_scope_from_db,
    resolve_admin_subject_workspace_id,
    resolve_actor_username_workspace_id,
    resolve_user_event_workspace_id,
    scope_for_admin_subject,
    scope_for_user_event,
)
from config import database_settings
from app.notification.queue_manager import DiscordNotification, TelegramNotification
from app.services.runtime_settings import clear_runtime_settings_cache
from sqlalchemy import select, text, update


async def _tenant(db) -> int:
    t = Tenant(name=f"nd_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"nd_{uuid.uuid4().hex[:10]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


async def _persist_user(
    db,
    admin: Admin | None,
    *,
    workspace_id: int | None = None,
) -> UserNotificationResponse:
    row = DbUser(
        username=f"u_{uuid.uuid4().hex[:8]}",
        status=UserStatus.active,
        admin_id=admin.id if admin is not None else None,
        workspace_id=workspace_id if workspace_id is not None else (admin.workspace_id if admin else None),
    )
    db.add(row)
    await db.flush()
    contact = build_admin_details(admin, include_loaded_metrics=False) if admin is not None else None
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


def _owner_actor() -> AdminDetails:
    return AdminDetails(
        username="platform_owner",
        role=AdminRoleData(name="owner", is_owner=True),
    )


def _bare_user_notification(
    *,
    user_id: int,
    admin: AdminContactInfo | None = None,
) -> UserNotificationResponse:
    return UserNotificationResponse(
        id=user_id,
        username=f"u_{uuid.uuid4().hex[:6]}",
        status=UserStatus.active,
        used_traffic=0,
        created_at="2024-01-01T00:00:00Z",
        data_limit_reset_strategy=DataLimitResetStrategy.no_reset,
        admin=admin,
        group_names=[],
    )


@pytest.fixture(autouse=True)
async def _reset_caches():
    await clear_runtime_settings_cache(all_scopes=True)
    yield
    await clear_runtime_settings_cache(all_scopes=True)


async def test_scope_uses_distinct_workspace_notification_tokens():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_b.workspace_id)
        row_a = await get_settings_by_workspace(db, admin_a.workspace_id)
        row_b = await get_settings_by_workspace(db, admin_b.workspace_id)
        row_a.notification_settings = {
            **row_a.notification_settings,
            "telegram_api_token": "1000000001:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "notify_telegram": True,
            "telegram_chat_id": 111,
        }
        row_b.notification_settings = {
            **row_b.notification_settings,
            "telegram_api_token": "1000000002:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            "notify_telegram": True,
            "telegram_chat_id": 222,
        }
        user_a = await _persist_user(db, admin_a)
        user_b = await _persist_user(db, admin_b)
        await db.commit()

        scope_a = await scope_for_user_event(user_a, AdminDetails(username="x", role=AdminRoleData()))
        scope_b = await scope_for_user_event(user_b, AdminDetails(username="y", role=AdminRoleData()))
        assert scope_a is not None and scope_b is not None
        assert scope_a.workspace_id == admin_a.workspace_id
        assert scope_b.workspace_id == admin_b.workspace_id
        assert scope_a.notification_settings.telegram_api_token != scope_b.notification_settings.telegram_api_token


async def test_missing_workspace_settings_fail_closed_no_enqueue():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        details = AdminDetails(
            username=admin.username,
            id=admin.id,
            workspace_id=admin.workspace_id,
            role=AdminRoleData(),
        )
        user = await _persist_user(db, admin)
        await db.commit()
        scope = await scope_for_user_event(user, details)
        assert scope is None

        with patch("app.notification.telegram.user.send_telegram_message", new_callable=AsyncMock) as mock_tg:
            with patch("app.notification.discord.user.send_discord_webhook", new_callable=AsyncMock) as mock_ds:
                await create_user(user, details)
                mock_tg.assert_not_awaited()
                mock_ds.assert_not_awaited()


async def test_create_user_enqueues_with_workspace_id():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin.workspace_id)
        row = await get_settings_by_workspace(db, admin.workspace_id)
        from app.models.notification_enable import NotificationEnable

        row.notification_enable = NotificationEnable().model_dump()
        row.notification_settings = {
            **row.notification_settings,
            "notify_telegram": True,
            "telegram_api_token": "12345678:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
            "telegram_chat_id": 333,
            "max_retries": 3,
        }
        await db.commit()
        await clear_runtime_settings_cache(all_scopes=True)

        details = AdminDetails(
            username=admin.username,
            id=admin.id,
            workspace_id=admin.workspace_id,
            role=AdminRoleData(),
        )
        user = await _persist_user(db, admin)
        await db.commit()

        scope = await scope_for_user_event(user, details)
        assert scope is not None

        with patch("app.notification.claim_notification_slot", return_value=True):
            with patch("app.notification.telegram.user.send_telegram_message", new_callable=AsyncMock) as mock_tg:
                await create_user(user, details)
                assert mock_tg.await_count >= 1
                for call in mock_tg.await_args_list:
                    assert call.kwargs.get("delivery_workspace_id") == admin.workspace_id


async def test_process_notification_uses_workspace_snapshot_not_global_accessor():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin.workspace_id)
        row = await get_settings_by_workspace(db, admin.workspace_id)
        row.notification_settings = {
            **row.notification_settings,
            "notify_telegram": True,
            "telegram_api_token": "12345678:DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
            "telegram_chat_id": 444,
            "max_retries": 3,
        }
        await db.commit()

        item = TelegramNotification(
            message="hi",
            chat_id=444,
            delivery_workspace_id=admin.workspace_id,
        ).model_dump()

        with patch("app.settings.notification_settings", new_callable=AsyncMock) as global_settings:
            with patch(
                "app.notification.client._send_telegram_message_direct",
                new_callable=AsyncMock,
                return_value=True,
            ) as send_direct:
                await process_notification(item)
                global_settings.assert_not_awaited()
                send_direct.assert_awaited_once()
                assert send_direct.await_args.kwargs["telegram_api_token"] == "12345678:DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"


async def test_concurrent_workspace_delivery_isolation():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_b.workspace_id)
        row_a = await get_settings_by_workspace(db, admin_a.workspace_id)
        row_b = await get_settings_by_workspace(db, admin_b.workspace_id)
        row_a.notification_settings = {
            **row_a.notification_settings,
            "notify_telegram": True,
            "telegram_api_token": "1000000005:EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE",
            "telegram_chat_id": 555,
            "max_retries": 3,
        }
        row_b.notification_settings = {
            **row_b.notification_settings,
            "notify_telegram": True,
            "telegram_api_token": "1000000006:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
            "telegram_chat_id": 666,
            "max_retries": 3,
        }
        await db.commit()

        tokens = []

        async def capture_send(**kwargs):
            tokens.append(kwargs["telegram_api_token"])

        with patch("app.notification.client._send_telegram_message_direct", new_callable=AsyncMock, side_effect=capture_send):
                await asyncio.gather(
                    process_notification(
                        TelegramNotification(
                            message="a",
                            chat_id=555,
                            delivery_workspace_id=admin_a.workspace_id,
                        ).model_dump()
                    ),
                    process_notification(
                        TelegramNotification(
                            message="b",
                            chat_id=666,
                            delivery_workspace_id=admin_b.workspace_id,
                        ).model_dump()
                    ),
                )
        assert len(tokens) == 2
        assert tokens[0] != tokens[1]


async def test_disabled_tenant_notification_prevents_delivery():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin.workspace_id)
        row = await get_settings_by_workspace(db, admin.workspace_id)
        from app.models.notification_enable import NotificationEnable, UserNotificationEnable

        row.notification_enable = NotificationEnable(user=UserNotificationEnable(create=False)).model_dump()
        await db.commit()
        details = AdminDetails(
            username=admin.username,
            id=admin.id,
            workspace_id=admin.workspace_id,
            role=AdminRoleData(),
        )
        user = await _persist_user(db, admin)
        await db.commit()
        with patch("app.notification.telegram.user.send_telegram_message", new_callable=AsyncMock) as mock_tg:
            await create_user(user, details)
            mock_tg.assert_not_awaited()


async def test_tenant_user_resolves_to_own_workspace_from_database():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = await _persist_user(db, admin)
        await db.commit()
        resolved = await resolve_user_event_workspace_id(user, _owner_actor())
        assert resolved == admin.workspace_id


async def test_tenant_admin_missing_workspace_fails_closed_db():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        admin.workspace_id = None
        user = await _persist_user(db, admin, workspace_id=None)
        await db.commit()
        assert await resolve_user_event_workspace_id(user, _owner_actor()) == "skip"


async def test_user_linked_to_tenant_admin_resolves_admin_workspace():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = await _persist_user(db, admin, workspace_id=None)
        await db.commit()
        resolved = await resolve_user_event_workspace_id(user, _owner_actor())
        assert resolved == admin.workspace_id


async def test_orphan_user_without_admin_id_fails_closed_db():
    async with GetDB() as db:
        user = await _persist_user(db, None, workspace_id=None)
        await db.commit()
        assert await resolve_user_event_workspace_id(user, _owner_actor()) == "skip"


async def test_missing_user_record_does_not_resolve_global():
    user = _bare_user_notification(user_id=99_999_999, admin=None)
    assert await resolve_user_event_workspace_id(user, _owner_actor()) == "skip"


async def test_lookup_linked_admin_id_matches_existing_admin_row():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = await _persist_user(db, admin)
        await db.commit()
        scope = await _lookup_user_owner_scope(user.id)
        assert scope is not None
        assert scope.user_admin_id == admin.id
        assert scope.linked_admin_id == admin.id
        assert scope.admin_tenant_id == tenant_id


@pytest.mark.skipif(not database_settings.is_sqlite, reason="SQLite FK pragma required to simulate dangling admin_id")
async def test_dangling_admin_id_fails_closed_via_actual_query():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = await _persist_user(db, admin)
        await db.commit()
        uid = user.id
        bad_admin_id = 8_000_000 + uid

    async with GetDB() as db:
        await db.execute(text("PRAGMA foreign_keys=OFF"))
        await db.execute(update(DbUser).where(DbUser.id == uid).values(admin_id=bad_admin_id))
        await db.commit()

    scope = await _lookup_user_owner_scope(uid)
    assert scope is not None
    assert scope.user_admin_id == bad_admin_id
    assert scope.linked_admin_id is None

    payload = _bare_user_notification(user_id=uid, admin=None)
    assert await resolve_user_event_workspace_id(payload, _owner_actor()) == "skip"


async def test_dto_tenant_claim_conflicts_with_platform_admin_db():
    async with GetDB() as db:
        platform_admin = await _owner_admin(db)
        user = await _persist_user(db, platform_admin, workspace_id=None)
        await db.commit()
        dto_user = _bare_user_notification(
            user_id=user.id,
            admin=AdminContactInfo(username="spoof", tenant_id=1, workspace_id=None),
        )
        assert await resolve_user_event_workspace_id(dto_user, _owner_actor()) == "skip"


async def test_dto_workspace_conflict_with_database_fails_closed():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = await _persist_user(db, admin)
        await db.commit()
        wrong_ws = (admin.workspace_id or 0) + 10_000
        dto_user = _bare_user_notification(
            user_id=user.id,
            admin=AdminContactInfo(
                username=admin.username,
                tenant_id=tenant_id,
                workspace_id=wrong_ws,
            ),
        )
        assert await resolve_user_event_workspace_id(dto_user, _owner_actor()) == "skip"


async def test_user_workspace_must_match_linked_admin_workspace():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        user = await _persist_user(db, admin)
        other_admin = await _administrator(db, tenant_id)
        await db.execute(
            update(DbUser).where(DbUser.id == user.id).values(workspace_id=other_admin.workspace_id)
        )
        await db.commit()
        payload = _bare_user_notification(
            user_id=user.id,
            admin=build_admin_details(admin, include_loaded_metrics=False),
        )
        assert await resolve_user_event_workspace_id(payload, _owner_actor()) == "skip"


async def test_user_workspace_cross_tenant_with_admin_fails_closed():
    async with GetDB() as db:
        tenant_a = await _tenant(db)
        tenant_b = await _tenant(db)
        admin_a = await _administrator(db, tenant_a)
        admin_b = await _administrator(db, tenant_b)
        user = await _persist_user(db, admin_a)
        await db.execute(
            update(DbUser).where(DbUser.id == user.id).values(workspace_id=admin_b.workspace_id)
        )
        await db.commit()
        payload = _bare_user_notification(
            user_id=user.id,
            admin=build_admin_details(admin_a, include_loaded_metrics=False),
        )
        assert await resolve_user_event_workspace_id(payload, _owner_actor()) == "skip"


async def test_global_owner_role_comes_from_database_not_dto():
    async with GetDB() as db:
        platform_admin = await _owner_admin(db)
        user = await _persist_user(db, platform_admin, workspace_id=None)
        await db.commit()
        dto_user = _bare_user_notification(
            user_id=user.id,
            admin=AdminContactInfo(
                username=platform_admin.username,
                tenant_id=None,
                workspace_id=None,
            ),
        )
        scope = await _lookup_user_owner_scope(user.id)
        assert scope is not None and scope.admin_is_owner is True
        assert await resolve_user_event_workspace_id(dto_user, _owner_actor()) is None
        operator_role = (await db.execute(select(AdminRole).where(AdminRole.name == "operator"))).scalar_one()
        platform_admin.role_id = operator_role.id
        await db.commit()
        assert (await _lookup_user_owner_scope(user.id)).admin_is_owner is False
        assert await resolve_user_event_workspace_id(dto_user, _owner_actor()) == "skip"


async def test_platform_global_user_requires_owner_role_in_database():
    async with GetDB() as db:
        platform_admin = await _owner_admin(db)
        user = await _persist_user(db, platform_admin, workspace_id=None)
        await db.commit()
        scope = await _lookup_user_owner_scope(user.id)
        assert scope is not None
        assert scope.admin_is_owner is True
        assert await resolve_user_event_workspace_id(user, _owner_actor()) is None


async def test_non_owner_linked_admin_without_workspace_fails_closed():
    async with GetDB() as db:
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
        user = await _persist_user(db, admin, workspace_id=None)
        await db.commit()
        assert await resolve_user_event_workspace_id(user, _owner_actor()) == "skip"


async def test_database_lookup_error_fails_closed_not_global():
    user = _bare_user_notification(user_id=1, admin=None)
    with patch(
        "app.notification.delivery_scope._lookup_user_owner_scope",
        new_callable=AsyncMock,
        side_effect=RuntimeError("db unavailable"),
    ):
        assert await resolve_user_event_workspace_id(user, _owner_actor()) == "skip"


async def test_orphan_user_does_not_enqueue_with_null_delivery_workspace():
    async with GetDB() as db:
        user = await _persist_user(db, None, workspace_id=None)
        await db.commit()
        with patch("app.notification.claim_notification_slot", return_value=True):
            with patch("app.notification.telegram.user.send_telegram_message", new_callable=AsyncMock) as mock_tg:
                with patch("app.notification.discord.user.send_discord_webhook", new_callable=AsyncMock) as mock_ds:
                    with patch("app.notification.client.enqueue_telegram", new_callable=AsyncMock) as mock_eq:
                        await create_user(user, _owner_actor())
                        mock_tg.assert_not_awaited()
                        mock_ds.assert_not_awaited()
                        mock_eq.assert_not_awaited()


def test_resolve_user_owner_scope_from_db_dangling_never_global():
    synthetic = _UserOwnerScope(
        user_workspace_id=None,
        user_workspace_tenant_id=None,
        user_admin_id=42,
        linked_admin_id=None,
        admin_tenant_id=None,
        admin_workspace_id=None,
        admin_is_owner=False,
    )
    assert _resolve_user_owner_scope_from_db(synthetic, dto_admin_tenant=None, dto_admin_workspace=None) == "skip"


async def test_admin_subject_owner_and_system_actor_remain_global():
    owner_admin = AdminDetails(
        username="owner",
        role=AdminRoleData(name="owner", is_owner=True),
        workspace_id=None,
        tenant_id=None,
    )
    assert resolve_admin_subject_workspace_id(owner_admin) is None
    assert await resolve_actor_username_workspace_id("system") is None
    with patch(
        "app.notification.delivery_scope.load_event_delivery_scope",
        new_callable=AsyncMock,
        return_value=object(),
    ) as load_scope:
        assert await scope_for_admin_subject(owner_admin) is not None
        load_scope.assert_awaited_once_with(None)


async def test_admin_login_still_uses_global_enable_path():
    from app.models.notification_enable import NotificationEnable

    enable = NotificationEnable.model_validate({})
    with patch("app.notification.notification_enable", new_callable=AsyncMock, return_value=enable):
        with patch("app.notification.claim_notification_slot", return_value=True):
            with patch("app.notification.tg.admin_login", new_callable=AsyncMock) as mock_login:
                with patch("app.notification.ds.admin_login", new_callable=AsyncMock):
                    from app.notification import admin_login

                    await admin_login("u", "p", "1.2.3.4", True)
                    mock_login.assert_awaited_once()
