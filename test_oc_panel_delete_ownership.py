"""Panel / tenant cleanup must delete only OC users owned via OCUserMapping (never bulk remote wipe)."""
from __future__ import annotations

import re
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select

from app.db import GetDB
from app.db.models import Tenant, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCSyncState, OCUserMapping
from app.jobs.process_oc_sync import process_oc_sync
from app.node.oc_sync import enqueue_panel_user_deletions, enqueue_tenant_panel_user_deletions
from app.services.oc_user_mapping_state import OC_MAPPING_STATUS_ACTIVE, OC_MAPPING_STATUS_DELETED

PASARGUARD_USER_A = "pg-owned-user-a"
PASARGUARD_USER_B = "pg-owned-user-b"
UNMANAGED_USER_C = "external-unmanaged-c"
UNMANAGED_USER_D = "external-unmanaged-d"


async def _teardown_fixture(
    db,
    *,
    panel_ids: tuple[int, ...] = (),
    user_ids: tuple[int, ...] = (),
    integration_ids: tuple[int, ...] = (),
    tenant_ids: tuple[int, ...] = (),
    sync_job_ids: tuple[int, ...] = (),
) -> None:
    if sync_job_ids:
        await db.execute(delete(OCSyncState).where(OCSyncState.id.in_(sync_job_ids)))
    if panel_ids:
        await db.execute(delete(OCUserMapping).where(OCUserMapping.panel_id.in_(panel_ids)))
        await db.execute(delete(OCPanel).where(OCPanel.id.in_(panel_ids)))
    if user_ids:
        await db.execute(delete(User).where(User.id.in_(user_ids)))
    if integration_ids:
        await db.execute(delete(OCIntegration).where(OCIntegration.id.in_(integration_ids)))
    if tenant_ids:
        await db.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
    await db.commit()


class _MockDeleteResponse:
    status = 204

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_panel_delete_enqueues_only_owned_mapping_external_user_ids():
    """Remote users C/D are not in OCUserMapping — no delete jobs for them."""
    panel_id = integration_id = tenant_id = 0
    user_ids: list[int] = []
    job_ids: list[int] = []
    async with GetDB() as db:
        tenant = Tenant(name=f"own_{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.flush()
        intg = OCIntegration(
            base_url="http://ownership-test",
            api_token_encrypted="enc",
            token_preview="own",
            is_active=True,
        )
        db.add(intg)
        await db.flush()
        source_panel_id = f"panel-own-{uuid.uuid4().hex[:10]}"
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id=source_panel_id,
            purchaser_identity="1",
            name="Panel 86",
            tenant_id=tenant.id,
            sync_status="connected",
        )
        db.add(panel)
        await db.flush()
        panel_id = panel.id
        integration_id = intg.id
        tenant_id = tenant.id

        u1 = User(username=f"u1_{uuid.uuid4().hex[:6]}", status=UserStatus.active)
        u2 = User(username=f"u2_{uuid.uuid4().hex[:6]}", status=UserStatus.active)
        db.add_all([u1, u2])
        await db.flush()
        user_ids = [u1.id, u2.id]

        db.add_all(
            [
                OCUserMapping(
                    user_id=u1.id,
                    panel_id=panel_id,
                    external_user_id=PASARGUARD_USER_A,
                    status=OC_MAPPING_STATUS_ACTIVE,
                    last_synced_configs=["cfg-a"],
                ),
                OCUserMapping(
                    user_id=u2.id,
                    panel_id=panel_id,
                    external_user_id=PASARGUARD_USER_B,
                    status=OC_MAPPING_STATUS_ACTIVE,
                    last_synced_configs=["cfg-b"],
                ),
            ]
        )
        await db.commit()

        await enqueue_panel_user_deletions(db, panel_id)
        await db.commit()

        entity_ids = {f"{u1.id}_{panel_id}", f"{u2.id}_{panel_id}"}
        jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_type == "user_mapping",
                    OCSyncState.operation == "delete",
                    OCSyncState.status == "pending",
                    OCSyncState.entity_id.in_(entity_ids),
                )
            )
        ).scalars().all()
        enqueued_ext_ids = {j.payload["external_user_id"] for j in jobs if j.payload}
        assert enqueued_ext_ids == {PASARGUARD_USER_A, PASARGUARD_USER_B}
        assert UNMANAGED_USER_C not in enqueued_ext_ids
        assert UNMANAGED_USER_D not in enqueued_ext_ids
        assert len(jobs) == 2
        job_ids = [j.id for j in jobs]
    async with GetDB() as db:
        await _teardown_fixture(
            db,
            panel_ids=(panel_id,),
            user_ids=tuple(user_ids),
            integration_ids=(integration_id,),
            tenant_ids=(tenant_id,),
            sync_job_ids=tuple(job_ids),
        )


@pytest.mark.asyncio
async def test_process_oc_sync_delete_calls_are_per_owned_external_user_id_only():
    deleted_urls: list[str] = []
    bulk_panel_wipe_called = False

    def _delete(url, *args, **kwargs):
        nonlocal bulk_panel_wipe_called
        deleted_urls.append(url)
        if re.search(r"/panels/[^/]+/users/?$", url.rstrip("/")) and "/users/" not in url.rstrip("/").split("/users/")[-1]:
            bulk_panel_wipe_called = True
        if url.rstrip("/").endswith("/users") or "/users/bulk" in url:
            bulk_panel_wipe_called = True
        return _MockDeleteResponse()

    panel_id = integration_id = tenant_id = 0
    user_ids: list[int] = []
    job_ids: list[int] = []
    async with GetDB() as db:
        tenant = Tenant(name=f"own2_{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.flush()
        intg = OCIntegration(
            base_url="http://ownership-test",
            api_token_encrypted="enc",
            token_preview="own",
            is_active=True,
        )
        db.add(intg)
        await db.flush()
        source_panel_id = f"panel-own-{uuid.uuid4().hex[:10]}"
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id=source_panel_id,
            purchaser_identity="1",
            name="Panel 86",
            tenant_id=tenant.id,
            sync_status="connected",
        )
        db.add(panel)
        await db.flush()
        panel_id = panel.id
        integration_id = intg.id
        tenant_id = tenant.id

        u1 = User(username=f"u1_{uuid.uuid4().hex[:6]}", status=UserStatus.active)
        u2 = User(username=f"u2_{uuid.uuid4().hex[:6]}", status=UserStatus.active)
        db.add_all([u1, u2])
        await db.flush()
        user_ids = [u1.id, u2.id]

        db.add_all(
            [
                OCUserMapping(
                    user_id=u1.id,
                    panel_id=panel_id,
                    external_user_id=PASARGUARD_USER_A,
                    status=OC_MAPPING_STATUS_ACTIVE,
                    last_synced_configs=["cfg-a"],
                ),
                OCUserMapping(
                    user_id=u2.id,
                    panel_id=panel_id,
                    external_user_id=PASARGUARD_USER_B,
                    status=OC_MAPPING_STATUS_ACTIVE,
                    last_synced_configs=["cfg-b"],
                ),
            ]
        )
        await db.commit()

        await enqueue_panel_user_deletions(db, panel_id)
        await db.commit()

        with patch("aiohttp.ClientSession.delete", side_effect=_delete), patch(
            "app.jobs.process_oc_sync.decrypt_secret", return_value="tok"
        ), patch(
            "app.jobs.process_oc_sync.connection_token_for_tenant", return_value="conn"
        ):
            for _ in range(5):
                await process_oc_sync()

        our_delete_urls = [u for u in deleted_urls if f"/panels/{source_panel_id}/users/" in u]
        deleted_ext_ids = set()
        for url in our_delete_urls:
            m = re.search(r"/users/([^/?]+)", url)
            if m:
                deleted_ext_ids.add(m.group(1))

        assert deleted_ext_ids == {PASARGUARD_USER_A, PASARGUARD_USER_B}
        assert UNMANAGED_USER_C not in deleted_ext_ids
        assert UNMANAGED_USER_D not in deleted_ext_ids
        assert bulk_panel_wipe_called is False
        assert len(our_delete_urls) == 2

        entity_ids = {f"{u1.id}_{panel_id}", f"{u2.id}_{panel_id}"}
        our_jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id.in_(entity_ids),
                    OCSyncState.operation == "delete",
                )
            )
        ).scalars().all()
        job_ids = [j.id for j in our_jobs]
    async with GetDB() as db:
        await _teardown_fixture(
            db,
            panel_ids=(panel_id,),
            user_ids=tuple(user_ids),
            integration_ids=(integration_id,),
            tenant_ids=(tenant_id,),
            sync_job_ids=tuple(job_ids),
        )


@pytest.mark.asyncio
async def test_tombstone_mapping_does_not_enqueue_repeat_delete():
    panel_id = integration_id = tenant_id = user_id = 0
    async with GetDB() as db:
        tenant = Tenant(name=f"tomb_{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.flush()
        intg = OCIntegration(
            base_url="http://ownership-test",
            api_token_encrypted="enc",
            token_preview="own",
            is_active=True,
        )
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="p1",
            purchaser_identity="1",
            name="p",
            tenant_id=tenant.id,
        )
        user = User(username=f"u_{uuid.uuid4().hex[:6]}", status=UserStatus.active)
        db.add_all([panel, user])
        await db.flush()
        panel_id = panel.id
        integration_id = intg.id
        tenant_id = tenant.id
        user_id = user.id
        db.add(
            OCUserMapping(
                user_id=user_id,
                panel_id=panel_id,
                external_user_id=PASARGUARD_USER_A,
                status=OC_MAPPING_STATUS_DELETED,
                last_synced_configs=[],
            )
        )
        await db.commit()

        await enqueue_panel_user_deletions(db, panel_id)
        await db.commit()

        entity_id = f"{user_id}_{panel_id}"
        jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_type == "user_mapping",
                    OCSyncState.operation == "delete",
                    OCSyncState.entity_id == entity_id,
                )
            )
        ).scalars().all()
        assert jobs == []

    async with GetDB() as db:
        await _teardown_fixture(
            db,
            panel_ids=(panel_id,),
            user_ids=(user_id,),
            integration_ids=(integration_id,),
            tenant_ids=(tenant_id,),
        )


@pytest.mark.asyncio
async def test_tenant_disconnect_enqueue_scoped_to_tenant_panel_mappings_only():
    panel_ids: list[int] = []
    user_ids: list[int] = []
    integration_id = tenant_id = other_tenant_id = 0
    job_ids: list[int] = []
    async with GetDB() as db:
        tenant = Tenant(name=f"td_{uuid.uuid4().hex[:8]}")
        other = Tenant(name=f"oth_{uuid.uuid4().hex[:8]}")
        db.add_all([tenant, other])
        await db.flush()
        intg = OCIntegration(
            base_url="http://ownership-test",
            api_token_encrypted="enc",
            token_preview="own",
            is_active=True,
        )
        db.add(intg)
        await db.flush()
        panel_owned = OCPanel(
            integration_id=intg.id,
            source_panel_id="owned",
            purchaser_identity="1",
            name="owned",
            tenant_id=tenant.id,
        )
        panel_other = OCPanel(
            integration_id=intg.id,
            source_panel_id="other",
            purchaser_identity="2",
            name="other",
            tenant_id=other.id,
        )
        db.add_all([panel_owned, panel_other])
        await db.flush()
        panel_ids = [panel_owned.id, panel_other.id]
        integration_id = intg.id
        tenant_id = tenant.id
        other_tenant_id = other.id
        u1 = User(username=f"u1_{uuid.uuid4().hex[:6]}", status=UserStatus.active)
        u2 = User(username=f"u2_{uuid.uuid4().hex[:6]}", status=UserStatus.active)
        db.add_all([u1, u2])
        await db.flush()
        user_ids = [u1.id, u2.id]
        db.add_all(
            [
                OCUserMapping(
                    user_id=u1.id,
                    panel_id=panel_ids[0],
                    external_user_id=PASARGUARD_USER_A,
                    status=OC_MAPPING_STATUS_ACTIVE,
                    last_synced_configs=["c"],
                ),
                OCUserMapping(
                    user_id=u2.id,
                    panel_id=panel_ids[1],
                    external_user_id=UNMANAGED_USER_C,
                    status=OC_MAPPING_STATUS_ACTIVE,
                    last_synced_configs=["c"],
                ),
            ]
        )
        await db.commit()

        await enqueue_tenant_panel_user_deletions(db, tenant.id)
        await db.commit()

        entity_id_owned = f"{u1.id}_{panel_ids[0]}"
        jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.operation == "delete",
                    OCSyncState.status == "pending",
                    OCSyncState.entity_id == entity_id_owned,
                )
            )
        ).scalars().all()
        ext_ids = {j.payload.get("external_user_id") for j in jobs}
        assert ext_ids == {PASARGUARD_USER_A}
        assert UNMANAGED_USER_C not in ext_ids

        other_entity = f"{u2.id}_{panel_ids[1]}"
        other_jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.operation == "delete",
                    OCSyncState.status == "pending",
                    OCSyncState.entity_id == other_entity,
                )
            )
        ).scalars().all()
        assert other_jobs == []

        job_ids = [j.id for j in jobs]
    async with GetDB() as db:
        await _teardown_fixture(
            db,
            panel_ids=tuple(panel_ids),
            user_ids=tuple(user_ids),
            integration_ids=(integration_id,),
            tenant_ids=(tenant_id, other_tenant_id),
            sync_job_ids=tuple(job_ids),
        )
