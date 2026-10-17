"""
Task 3.1 — Telegram disconnect lifecycle (requires app database).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.db import GetDB
from app.db.base import engine
from app.db.models import Tenant, User, UserStatus
from app.db.models_oc import (
    OCIntegration,
    OCPanel,
    OCSyncState,
    OCUserMapping,
    TenantTelegramConnection,
    TenantTelegramConnectionStatus,
)
from app.jobs.process_oc_sync import process_oc_sync
from app.node.oc_sync import enqueue_oc_user_sync, enqueue_tenant_panel_user_deletions
from app.models.admin import AdminDetails, AdminRoleData
from app.services.oc_telegram_connection import (
    get_active_connection,
    require_active_telegram_for_tenant_admin,
    revoke_connection,
)


class _AdminStub:
    def __init__(self, tenant_id: int | None):
        self.tenant_id = tenant_id


async def _dispose_db_engine() -> None:
    await engine.dispose()


async def _cleanup_fixture_ids(
    db,
    *,
    sync_job_ids: list[int] = (),
    mapping_ids: list[int] = (),
    connection_ids: list[int] = (),
    panel_ids: list[int] = (),
    user_ids: list[int] = (),
    integration_ids: list[int] = (),
    tenant_ids: list[int] = (),
) -> None:
    if sync_job_ids:
        await db.execute(delete(OCSyncState).where(OCSyncState.id.in_(sync_job_ids)))
    if mapping_ids:
        await db.execute(delete(OCUserMapping).where(OCUserMapping.id.in_(mapping_ids)))
    if connection_ids:
        await db.execute(
            delete(TenantTelegramConnection).where(TenantTelegramConnection.id.in_(connection_ids))
        )
    if panel_ids:
        await db.execute(delete(OCPanel).where(OCPanel.id.in_(panel_ids)))
    if user_ids:
        await db.execute(delete(User).where(User.id.in_(user_ids)))
    if integration_ids:
        await db.execute(delete(OCIntegration).where(OCIntegration.id.in_(integration_ids)))
    if tenant_ids:
        await db.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
    await db.commit()


@pytest.mark.asyncio
async def test_require_active_telegram_blocks_disconnected_tenant_admin():
    admin = AdminDetails(
        id=1,
        username="tenant-admin",
        tenant_id=1,
        role=AdminRoleData(id=2, name="administrator", is_owner=False),
    )
    db = AsyncMock()
    with patch(
        "app.services.oc_actor_connection.require_oc_telegram_connection_for_actor",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=403, detail="No active Telegram connection"),
    ):
        with pytest.raises(HTTPException) as exc:
            await require_active_telegram_for_tenant_admin(db, admin, is_owner=False)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_active_telegram_allows_owner_without_connection():
    admin = _AdminStub(tenant_id=1)
    db = AsyncMock()
    await require_active_telegram_for_tenant_admin(db, admin, is_owner=True)


@pytest.mark.asyncio
async def test_revoke_enqueues_delete_jobs_for_tenant_mappings():
    fixture_ids: dict[str, list[int]] = {
        "tenant_ids": [],
        "integration_ids": [],
        "panel_ids": [],
        "user_ids": [],
        "mapping_ids": [],
        "connection_ids": [],
        "sync_job_ids": [],
    }
    try:
        async with GetDB() as db:
            tenant_a = Tenant(name=f"t-a-{uuid.uuid4().hex[:8]}")
            tenant_b = Tenant(name=f"t-b-{uuid.uuid4().hex[:8]}")
            db.add_all([tenant_a, tenant_b])
            await db.flush()
            fixture_ids["tenant_ids"].extend([tenant_a.id, tenant_b.id])

            intg = OCIntegration(
                base_url="http://lifecycle-test",
                api_token_encrypted="enc",
                token_preview="lc",
                is_active=True,
            )
            db.add(intg)
            await db.flush()
            fixture_ids["integration_ids"].append(intg.id)

            panel_a = OCPanel(
                integration_id=intg.id,
                source_panel_id="sub-a",
                purchaser_identity="1",
                name="Panel A",
                tenant_id=tenant_a.id,
                oc_account_id=100,
            )
            panel_b = OCPanel(
                integration_id=intg.id,
                source_panel_id="sub-b",
                purchaser_identity="2",
                name="Panel B",
                tenant_id=tenant_b.id,
                oc_account_id=200,
            )
            db.add_all([panel_a, panel_b])
            await db.flush()
            fixture_ids["panel_ids"].extend([panel_a.id, panel_b.id])

            user_a = User(username=f"u-a-{uuid.uuid4().hex[:6]}", status=UserStatus.active)
            user_b = User(username=f"u-b-{uuid.uuid4().hex[:6]}", status=UserStatus.active)
            db.add_all([user_a, user_b])
            await db.flush()
            fixture_ids["user_ids"].extend([user_a.id, user_b.id])

            ext_a = str(uuid.uuid4())
            ext_b = str(uuid.uuid4())
            map_a = OCUserMapping(
                user_id=user_a.id,
                panel_id=panel_a.id,
                external_user_id=ext_a,
                status="active",
                last_synced_configs=["cfg1"],
            )
            map_b = OCUserMapping(
                user_id=user_b.id,
                panel_id=panel_b.id,
                external_user_id=ext_b,
                status="active",
                last_synced_configs=["cfg2"],
            )
            db.add_all([map_a, map_b])
            await db.flush()
            fixture_ids["mapping_ids"].extend([map_a.id, map_b.id])

            conn_a = TenantTelegramConnection(
                tenant_id=tenant_a.id,
                telegram_user_id=111,
                oc_account_id=100,
                status=TenantTelegramConnectionStatus.active.value,
                active=True,
                verified_at=dt.now(UTC),
                connected_at=dt.now(UTC),
            )
            conn_b = TenantTelegramConnection(
                tenant_id=tenant_b.id,
                telegram_user_id=222,
                oc_account_id=200,
                status=TenantTelegramConnectionStatus.active.value,
                active=True,
                verified_at=dt.now(UTC),
                connected_at=dt.now(UTC),
            )
            db.add_all([conn_a, conn_b])
            await db.flush()
            fixture_ids["connection_ids"].extend([conn_a.id, conn_b.id])

            await revoke_connection(db, tenant_a.id)
            await db.commit()

            jobs_a = (
                await db.execute(
                    select(OCSyncState).where(
                        OCSyncState.entity_id == f"{user_a.id}_{panel_a.id}",
                        OCSyncState.operation == "delete",
                        OCSyncState.status == "pending",
                    )
                )
            ).scalars().all()
            assert len(jobs_a) == 1
            # Delete payload also snapshots who may authorise the delete on OC after the
            # OCPanel row is gone (tenant/account) and the revoked connection it must use.
            assert jobs_a[0].payload == {
                "external_user_id": ext_a,
                "source_panel_id": "sub-a",
                "integration_id": intg.id,
                "tenant_id": tenant_a.id,
                "oc_account_id": 100,
                "connection_id": conn_a.id,
            }
            fixture_ids["sync_job_ids"].append(jobs_a[0].id)

            jobs_b = (
                await db.execute(
                    select(OCSyncState).where(
                        OCSyncState.entity_id == f"{user_b.id}_{panel_b.id}",
                        OCSyncState.operation == "delete",
                    )
                )
            ).scalars().all()
            assert len(jobs_b) == 0

            assert await get_active_connection(db, tenant_a.id) is None
            assert await get_active_connection(db, tenant_b.id) is not None
    finally:
        async with GetDB() as db:
            await _cleanup_fixture_ids(db, **fixture_ids)
        await _dispose_db_engine()
        await _dispose_db_engine()


@pytest.mark.asyncio
async def test_enqueue_oc_user_sync_skips_create_when_tenant_disconnected():
    fixture_ids: dict[str, list[int]] = {
        "tenant_ids": [],
        "integration_ids": [],
        "panel_ids": [],
        "user_ids": [],
        "mapping_ids": [],
        "connection_ids": [],
        "sync_job_ids": [],
    }
    try:
        async with GetDB() as db:
            tenant = Tenant(name=f"t-disc-{uuid.uuid4().hex[:8]}")
            db.add(tenant)
            await db.flush()
            fixture_ids["tenant_ids"].append(tenant.id)

            intg = OCIntegration(
                base_url="http://disc-sync",
                api_token_encrypted="enc",
                token_preview="ds",
                is_active=True,
            )
            db.add(intg)
            await db.flush()
            fixture_ids["integration_ids"].append(intg.id)

            panel = OCPanel(
                integration_id=intg.id,
                source_panel_id="sub-d",
                purchaser_identity="9",
                name="Disc Panel",
                tenant_id=tenant.id,
                oc_account_id=9,
            )
            db.add(panel)
            await db.flush()
            fixture_ids["panel_ids"].append(panel.id)

            revoked = TenantTelegramConnection(
                tenant_id=tenant.id,
                telegram_user_id=333,
                oc_account_id=9,
                status=TenantTelegramConnectionStatus.revoked.value,
                active=False,
                revoked_at=dt.now(UTC),
            )
            db.add(revoked)
            await db.flush()
            fixture_ids["connection_ids"].append(revoked.id)

            user = User(username=f"u-disc-{uuid.uuid4().hex[:6]}", status=UserStatus.active)
            db.add(user)
            await db.flush()
            fixture_ids["user_ids"].append(user.id)

            with patch(
                "app.operation.access_control.check_user_access_allowed",
                new_callable=AsyncMock,
                return_value=True,
            ):
                with patch(
                    "app.node.oc_sync._inbounds_from_loaded_groups",
                    return_value={panel.id: {f"oc_{panel.id}_cfg"}},
                ):
                    with patch(
                        "app.node.oc_sync._bucket_inbounds",
                        return_value={panel.id: {f"oc_{panel.id}_cfg"}},
                    ):
                        await enqueue_oc_user_sync(db, user)
                        await db.commit()

            jobs = (
                await db.execute(
                    select(OCSyncState).where(
                        OCSyncState.entity_id == f"{user.id}_{panel.id}",
                    )
                )
            ).scalars().all()
            assert jobs == []

            mappings = (
                await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == user.id))
            ).scalars().all()
            assert mappings == []
    finally:
        async with GetDB() as db:
            await _cleanup_fixture_ids(db, **fixture_ids)
        await _dispose_db_engine()
        await _dispose_db_engine()


@pytest.mark.asyncio
async def test_worker_blocks_create_when_tenant_disconnected():
    fixture_ids: dict[str, list[int]] = {
        "tenant_ids": [],
        "integration_ids": [],
        "panel_ids": [],
        "user_ids": [],
        "mapping_ids": [],
        "connection_ids": [],
        "sync_job_ids": [],
    }
    job_id: int | None = None
    try:
        async with GetDB() as db:
            tenant = Tenant(name=f"t-wk-{uuid.uuid4().hex[:8]}")
            db.add(tenant)
            await db.flush()
            fixture_ids["tenant_ids"].append(tenant.id)

            intg = OCIntegration(
                base_url="http://worker-block",
                api_token_encrypted="enc",
                token_preview="wb",
                is_active=True,
            )
            db.add(intg)
            await db.flush()
            fixture_ids["integration_ids"].append(intg.id)

            panel = OCPanel(
                integration_id=intg.id,
                source_panel_id="sub-w",
                purchaser_identity="8",
                name="Worker Panel",
                tenant_id=tenant.id,
                oc_account_id=8,
            )
            db.add(panel)
            await db.flush()
            fixture_ids["panel_ids"].append(panel.id)

            user = User(username=f"u-wk-{uuid.uuid4().hex[:6]}", status=UserStatus.active)
            db.add(user)
            await db.flush()
            fixture_ids["user_ids"].append(user.id)

            ext = str(uuid.uuid4())
            mapping = OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id=ext,
                status="active",
            )
            db.add(mapping)
            await db.flush()
            fixture_ids["mapping_ids"].append(mapping.id)

            revoked = TenantTelegramConnection(
                tenant_id=tenant.id,
                status=TenantTelegramConnectionStatus.revoked.value,
                active=False,
                revoked_at=dt.now(UTC),
            )
            db.add(revoked)
            await db.flush()
            fixture_ids["connection_ids"].append(revoked.id)

            job = OCSyncState(
                entity_type="user_mapping",
                entity_id=f"{user.id}_{panel.id}",
                operation="create",
                idempotency_key=f"test_{uuid.uuid4()}",
                payload={"groups": [], "configs": ["c1"]},
                status="pending",
            )
            db.add(job)
            await db.commit()
            job_id = job.id
            fixture_ids["sync_job_ids"].append(job.id)

            mock_session = MagicMock()
            mock_session.request = MagicMock()

            with patch("aiohttp.ClientSession", return_value=mock_session):
                mock_session.__aenter__ = AsyncMock(return_value=mock_session)
                mock_session.__aexit__ = AsyncMock(return_value=None)
                await process_oc_sync()

            async with GetDB() as db2:
                refreshed = (
                    await db2.execute(select(OCSyncState).where(OCSyncState.id == job_id))
                ).scalar_one()
                assert refreshed.status == "completed"
                assert mock_session.request.call_count == 0
    finally:
        async with GetDB() as db:
            await _cleanup_fixture_ids(db, **fixture_ids)
        await _dispose_db_engine()
        await _dispose_db_engine()


@pytest.mark.asyncio
async def test_enqueue_tenant_deletions_idempotent_pending_delete():
    fixture_ids: dict[str, list[int]] = {
        "tenant_ids": [],
        "integration_ids": [],
        "panel_ids": [],
        "user_ids": [],
        "mapping_ids": [],
        "connection_ids": [],
        "sync_job_ids": [],
    }
    try:
        async with GetDB() as db:
            tenant = Tenant(name=f"t-idem-{uuid.uuid4().hex[:8]}")
            db.add(tenant)
            await db.flush()
            fixture_ids["tenant_ids"].append(tenant.id)

            intg = OCIntegration(
                base_url="http://idem",
                api_token_encrypted="enc",
                token_preview="id",
                is_active=True,
            )
            db.add(intg)
            await db.flush()
            fixture_ids["integration_ids"].append(intg.id)

            panel = OCPanel(
                integration_id=intg.id,
                source_panel_id="sub-i",
                purchaser_identity="3",
                name="Idem Panel",
                tenant_id=tenant.id,
            )
            db.add(panel)
            await db.flush()
            fixture_ids["panel_ids"].append(panel.id)

            user = User(username=f"u-idem-{uuid.uuid4().hex[:6]}", status=UserStatus.active)
            db.add(user)
            await db.flush()
            fixture_ids["user_ids"].append(user.id)

            ext = str(uuid.uuid4())
            mapping = OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id=ext,
                status="active",
                last_synced_configs=["x"],
            )
            db.add(mapping)
            await db.flush()
            fixture_ids["mapping_ids"].append(mapping.id)

            await enqueue_tenant_panel_user_deletions(db, tenant.id)
            await enqueue_tenant_panel_user_deletions(db, tenant.id)
            await db.commit()

            jobs = (
                await db.execute(
                    select(OCSyncState).where(
                        OCSyncState.entity_id == f"{user.id}_{panel.id}",
                        OCSyncState.operation == "delete",
                        OCSyncState.status == "pending",
                    )
                )
            ).scalars().all()
            assert len(jobs) == 1
            assert jobs[0].payload == {
                "external_user_id": ext,
                "source_panel_id": "sub-i",
                "integration_id": intg.id,
                "tenant_id": tenant.id,
            }
            fixture_ids["sync_job_ids"].append(jobs[0].id)
    finally:
        async with GetDB() as db:
            await _cleanup_fixture_ids(db, **fixture_ids)
        await _dispose_db_engine()
