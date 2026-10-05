"""
Regression tests for panel import wizard API (multi-panel, already-imported, Telegram lifecycle).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db.models_oc import OCPanel, TenantTelegramConnection, TenantTelegramConnectionStatus
from app.routers import integration as integ
from app.services import oc_telegram_connection as tg

# Reuse the scoped OC fake environment from the main integration suite.
from test_oc_scoped_integration import (  # noqa: E402
    OC_ACCOUNT,
    _add_connection,
    _add_panel,
    env as env_fixture,
)


@pytest.mark.asyncio
async def test_available_panels_marks_already_imported(env_fixture):
    await _add_connection(env_fixture)
    pid = await _add_panel(env_fixture, source="11")
    async with env_fixture.factory() as db:
        out = await integ.get_available_panels(db=db, user_context=env_fixture.ctx)
    by_id = {p.id: p for p in out.items}
    assert by_id[11].already_imported is True
    assert by_id[11].imported_panel_id == pid


@pytest.mark.asyncio
async def test_import_panels_idempotent_for_already_imported_subscription(env_fixture):
    await _add_connection(env_fixture)
    pid = await _add_panel(env_fixture, source="11")
    async with env_fixture.factory() as db:
        out = await integ.import_panels(
            body=integ.ImportPanelsRequest(subscription_ids=[11]),
            db=db,
            user_context=env_fixture.ctx,
        )
    assert len(out.imported) == 1
    assert out.imported[0].panel_id == pid
    assert out.imported[0].created is False


@pytest.mark.asyncio
async def test_import_panels_returns_multiple_panel_ids_for_sequential_wizard(env_fixture):
    await _add_connection(env_fixture)
    env_fixture.fake.account_panel_items = [
        {"subscription_id": 11, "panel_type": "PASARGUARD", "name": "A", "status": "ACTIVE"},
        {"subscription_id": 12, "panel_type": "PASARGUARD", "name": "B", "status": "ACTIVE"},
    ]
    async with env_fixture.factory() as db:
        out = await integ.import_panels(
            body=integ.ImportPanelsRequest(subscription_ids=[11, 12]),
            db=db,
            user_context=env_fixture.ctx,
        )
    assert [r.subscription_id for r in out.imported] == [11, 12]
    assert len({r.panel_id for r in out.imported}) == 2


@pytest.mark.asyncio
async def test_select_panel_rejects_duplicate_import(env_fixture):
    await _add_connection(env_fixture)
    await _add_panel(env_fixture, source="11")
    async with env_fixture.factory() as db:
        with pytest.raises(HTTPException) as exc:
            await integ.select_panel(
                req=integ.SelectPanelRequest(source_panel_id="11", name="A"),
                db=db,
                user_context=env_fixture.ctx,
            )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_telegram_disconnect_then_reconnect_without_duplicate_active(env_fixture):
    await _add_connection(env_fixture)
    async with env_fixture.factory() as db:
        await integ.telegram_connection_revoke(db=db, user_context=env_fixture.ctx)
        await db.commit()
        assert await tg.get_active_connection(
            db, env_fixture.tenant_id, binding_admin_id=env_fixture.admin.id
        ) is None

        start = await integ.telegram_connection_start(db=db, user_context=env_fixture.ctx)
        await db.commit()
        out = await integ.telegram_connection_confirm(
            body=integ.TelegramConnectionConfirmRequest(code="12345"),
            db=db,
            user_context=env_fixture.ctx,
        )
        await db.commit()
    assert out.active
    async with env_fixture.factory() as db:
        rows = (
            await db.execute(
                select(TenantTelegramConnection).where(
                    TenantTelegramConnection.tenant_id == env_fixture.tenant_id
                )
            )
        ).scalars().all()
        active_rows = [r for r in rows if r.status == TenantTelegramConnectionStatus.active.value and r.active]
    assert len(active_rows) == 1
