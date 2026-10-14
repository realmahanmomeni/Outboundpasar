"""Phase 2-A: workspace-scoped runtime settings reads and cache isolation."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.db import GetDB
from app.db.crud.settings import ensure_workspace_settings, get_global_settings, get_settings_by_workspace
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.services.runtime_settings import (
    WorkspaceSettingsNotFoundError,
    clear_runtime_settings_cache,
    get_runtime_settings_snapshot,
    parse_settings_refresh_message,
    runtime_settings_cache_key,
)
from app.settings import handle_settings_message, refresh_caches, subscription_settings
from sqlalchemy import select


async def _tenant(db) -> int:
    t = Tenant(name=f"rt_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int) -> Admin:
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=f"rt_{uuid.uuid4().hex[:10]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


@pytest.fixture(autouse=True)
async def _reset_runtime_settings_caches():
    await clear_runtime_settings_cache(all_scopes=True)
    await refresh_caches(workspace_id=None)
    yield
    await clear_runtime_settings_cache(all_scopes=True)
    await refresh_caches(workspace_id=None)


async def test_global_scope_returns_global_settings():
    async with GetDB() as db:
        global_row = await get_global_settings(db)
        assert global_row is not None
        snap = await get_runtime_settings_snapshot(None, db=db, cache_read=False)
        assert snap.workspace_id is None
        assert snap.settings.subscription is not None
        assert snap.settings.subscription.profile_title == global_row.subscription.get("profile_title")


async def test_workspace_scopes_return_distinct_settings():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_b.workspace_id)
        row_a = await get_settings_by_workspace(db, admin_a.workspace_id)
        row_b = await get_settings_by_workspace(db, admin_b.workspace_id)
        assert row_a is not None and row_b is not None
        row_a.subscription = {**row_a.subscription, "profile_title": "MARKER_WORKSPACE_A"}
        row_b.subscription = {**row_b.subscription, "profile_title": "MARKER_WORKSPACE_B"}
        await db.commit()

        snap_a = await get_runtime_settings_snapshot(admin_a.workspace_id, db=db, cache_read=False)
        snap_b = await get_runtime_settings_snapshot(admin_b.workspace_id, db=db, cache_read=False)
        assert snap_a.settings.subscription.profile_title == "MARKER_WORKSPACE_A"
        assert snap_b.settings.subscription.profile_title == "MARKER_WORKSPACE_B"


async def test_missing_workspace_does_not_return_global():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        missing_id = admin.workspace_id
        with pytest.raises(WorkspaceSettingsNotFoundError) as exc:
            await get_runtime_settings_snapshot(missing_id, db=db, cache_read=False)
        assert exc.value.workspace_id == missing_id


async def test_cache_isolated_between_workspaces():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_b.workspace_id)
        row_a = await get_settings_by_workspace(db, admin_a.workspace_id)
        row_b = await get_settings_by_workspace(db, admin_b.workspace_id)
        row_a.subscription = {**row_a.subscription, "profile_title": "CACHE_A"}
        row_b.subscription = {**row_b.subscription, "profile_title": "CACHE_B"}
        await db.commit()

        from app.services.runtime_settings import _load_runtime_settings_snapshot_from_db

        with patch(
            "app.services.runtime_settings._load_runtime_settings_snapshot_from_db",
            new_callable=AsyncMock,
        ) as mock_load:
            mock_load.side_effect = _load_runtime_settings_snapshot_from_db
            await get_runtime_settings_snapshot(admin_a.workspace_id)
            await get_runtime_settings_snapshot(admin_a.workspace_id)
            await get_runtime_settings_snapshot(admin_b.workspace_id)
            assert mock_load.await_count == 2

        snap_b = await get_runtime_settings_snapshot(admin_b.workspace_id)
        assert snap_b.settings.subscription.profile_title == "CACHE_B"


async def test_invalidate_workspace_a_does_not_clear_workspace_b_cache():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_b.workspace_id)
        row_a = await get_settings_by_workspace(db, admin_a.workspace_id)
        row_b = await get_settings_by_workspace(db, admin_b.workspace_id)
        row_a.subscription = {**row_a.subscription, "profile_title": "INV_A"}
        row_b.subscription = {**row_b.subscription, "profile_title": "INV_B"}
        await db.commit()

        await get_runtime_settings_snapshot(admin_a.workspace_id)
        await get_runtime_settings_snapshot(admin_b.workspace_id)

        from app.services.runtime_settings import _load_runtime_settings_snapshot_from_db

        with patch(
            "app.services.runtime_settings._load_runtime_settings_snapshot_from_db",
            new_callable=AsyncMock,
        ) as mock_load:
            mock_load.side_effect = _load_runtime_settings_snapshot_from_db

            await clear_runtime_settings_cache(workspace_id=admin_a.workspace_id)
            await get_runtime_settings_snapshot(admin_b.workspace_id)
            assert mock_load.await_count == 0

            await get_runtime_settings_snapshot(admin_a.workspace_id)
            assert mock_load.await_count == 1


async def test_global_invalidation_does_not_clear_workspace_snapshot_cache():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin.workspace_id)
        row = await get_settings_by_workspace(db, admin.workspace_id)
        row.subscription = {**row.subscription, "profile_title": "WS_PERSIST"}
        await db.commit()

        await get_runtime_settings_snapshot(admin.workspace_id)

        from app.services.runtime_settings import _load_runtime_settings_snapshot_from_db

        with patch(
            "app.services.runtime_settings._load_runtime_settings_snapshot_from_db",
            new_callable=AsyncMock,
        ) as mock_load:
            mock_load.side_effect = _load_runtime_settings_snapshot_from_db

            await refresh_caches(workspace_id=None)
            await get_runtime_settings_snapshot(admin.workspace_id)
            assert mock_load.await_count == 0


async def test_parse_legacy_global_refresh_message():
    event = parse_settings_refresh_message({"action": "refresh"})
    assert event is not None
    assert event.workspace_id is None
    assert event.legacy_global_message is True


async def test_parse_scoped_refresh_not_global():
    event = parse_settings_refresh_message({"action": "refresh", "workspace_id": 42})
    assert event is not None
    assert event.workspace_id == 42
    assert event.legacy_global_message is False


async def test_parse_explicit_null_global_refresh():
    event = parse_settings_refresh_message({"action": "refresh", "workspace_id": None})
    assert event is not None
    assert event.workspace_id is None
    assert event.legacy_global_message is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"workspace_id": 1},
        {"action": "nope"},
        {"action": "refresh", "workspace_id": "12"},
        {"action": "refresh", "workspace_id": True},
        {"action": "refresh", "workspace_id": 12.5},
        {"action": "refresh", "workspace_id": 0},
        {"action": "refresh", "workspace_id": -3},
        {"action": 1, "workspace_id": 1},
        "not-a-dict",
    ],
)
async def test_parse_rejects_malformed_refresh_messages(payload):
    assert parse_settings_refresh_message(payload) is None


async def test_handle_settings_message_rejected_payload_has_no_side_effects():
    with patch("app.settings.refresh_caches", new_callable=AsyncMock) as mock_refresh:
        await handle_settings_message({"action": "refresh", "workspace_id": "bad"})
        await handle_settings_message({"workspace_id": 1})
        await handle_settings_message({})
        mock_refresh.assert_not_awaited()


async def test_handle_settings_message_explicit_null_syncs_telegram_bot():
    with patch("app.settings.refresh_caches", new_callable=AsyncMock) as mock_refresh:
        await handle_settings_message({"action": "refresh", "workspace_id": None})
        mock_refresh.assert_awaited_once_with(workspace_id=None, sync_telegram_bot=True)


async def test_handle_settings_message_scoped_does_not_sync_telegram_bot():
    with patch("app.settings.refresh_caches", new_callable=AsyncMock) as mock_refresh:
        await handle_settings_message({"action": "refresh", "workspace_id": 99})
        mock_refresh.assert_awaited_once_with(workspace_id=99, sync_telegram_bot=False)


async def test_handle_settings_message_legacy_syncs_telegram_bot():
    with patch("app.settings.refresh_caches", new_callable=AsyncMock) as mock_refresh:
        await handle_settings_message({"action": "refresh"})
        mock_refresh.assert_awaited_once_with(workspace_id=None, sync_telegram_bot=True)


async def test_concurrent_reads_use_distinct_cache_keys():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_a.workspace_id)
        await ensure_workspace_settings(db, tenant_id=tenant_id, workspace_id=admin_b.workspace_id)
        row_a = await get_settings_by_workspace(db, admin_a.workspace_id)
        row_b = await get_settings_by_workspace(db, admin_b.workspace_id)
        row_a.subscription = {**row_a.subscription, "profile_title": "CONC_A"}
        row_b.subscription = {**row_b.subscription, "profile_title": "CONC_B"}
        await db.commit()

        results = await asyncio.gather(
            get_runtime_settings_snapshot(admin_a.workspace_id),
            get_runtime_settings_snapshot(admin_b.workspace_id),
            get_runtime_settings_snapshot(admin_a.workspace_id),
        )
        assert results[0].settings.subscription.profile_title == "CONC_A"
        assert results[1].settings.subscription.profile_title == "CONC_B"
        assert results[2].settings.subscription.profile_title == "CONC_A"
        assert runtime_settings_cache_key(admin_a.workspace_id) != runtime_settings_cache_key(admin_b.workspace_id)


async def test_legacy_global_accessor_still_reads_global_row():
    async with GetDB() as db:
        global_row = await get_global_settings(db)
        assert global_row is not None
        legacy = await subscription_settings()
        assert legacy.profile_title == global_row.subscription.get("profile_title")
