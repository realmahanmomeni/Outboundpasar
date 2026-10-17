"""OC desired-state reconciliation: repair, cleanup, and race safety."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import and_, delete, select
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Group, ProxyInbound, Tenant, TenantStatus, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCUserMapping, OCSyncState
from app.jobs.process_oc_sync import process_oc_sync
from app.node.oc_sync import enqueue_oc_user_sync, remove_imported_panel
from app.services.oc_user_mapping_state import OC_MAPPING_STATUS_ACTIVE, OC_MAPPING_STATUS_DELETED


class MockResponse:
    def __init__(self, status=200, json_body=None):
        self.status = status
        self._json_body = json_body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    def raise_for_status(self):
        if self.status >= 400:
            raise Exception("Mock HTTP Error")

    async def json(self):
        return self._json_body


def mock_put_with_sub(*args, **kwargs):
    return MockResponse(200, {"subscription_url": "https://1.1.1.1/sub/test-user"})


async def mock_decrypt(*args, **kwargs):
    return "decrypted_token"


async def _panel86_fixture(db):
    tenant = Tenant(name=f"recon_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    admin = Admin(
        username=f"adm_{uuid.uuid4().hex[:6]}",
        hashed_password="x",
        role_id=role.id,
        tenant_id=tenant.id,
    )
    db.add(admin)
    await db.flush()
    await create_workspace_for_administrator(db, admin)
    intg = OCIntegration(base_url="http://test", api_token_encrypted="e", token_preview="test")
    db.add(intg)
    await db.flush()
    panel = OCPanel(
        integration_id=intg.id,
        source_panel_id="1506",
        purchaser_identity="b",
        name="Panel 86 (testpg)",
        tenant_id=tenant.id,
        workspace_id=admin.workspace_id,
        sync_status="connected",
    )
    db.add(panel)
    await db.flush()
    cfg_id = f"cfg-{uuid.uuid4().hex[:8]}"
    tag_a = f"oc_{panel.id}_{cfg_id}_{uuid.uuid4().hex[:6]}"
    inbound_a = ProxyInbound(tag=tag_a)
    config = OCPanelConfig(
        panel_id=panel.id,
        source_config_id=cfg_id,
        source_name="A",
        virtual_inbound_tag=tag_a,
    )
    group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound_a])
    db.add_all([inbound_a, config, group])
    await db.flush()
    return tenant, admin, panel, group, tag_a, cfg_id


@pytest.mark.asyncio
async def test_missing_subscription_url_enqueues_repair_not_duplicate_user():
    async with GetDB() as db:
        _, admin, panel, group, _tag, cfg_id = await _panel86_fixture(db)
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        ext_id = str(uuid.uuid4())
        mapping = OCUserMapping(
            user_id=user.id,
            panel_id=panel.id,
            external_user_id=ext_id,
            status=OC_MAPPING_STATUS_ACTIVE,
            last_synced_configs=[cfg_id],
        )
        db.add(mapping)
        await db.commit()

        with patch("app.node.oc_sync.tenant_panel_allows_oc_user_mutations", return_value=True):
            await enqueue_oc_user_sync(db, user)
        await db.commit()

        job = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id == f"{user.id}_{panel.id}",
                    OCSyncState.status == "pending",
                )
            )
        ).scalar_one_or_none()
        assert job is not None
        assert job.operation in ("create", "update")
        m2 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == mapping.id))).scalar_one()
        assert m2.external_user_id == ext_id

        await db.execute(delete(OCSyncState).where(OCSyncState.id == job.id))
        await db.delete(mapping)
        await db.delete(user)
        await db.commit()


@pytest.mark.asyncio
async def test_deleted_mapping_not_recreated_when_panel_not_desired():
    async with GetDB() as db:
        _, admin, panel, _, _, _ = await _panel86_fixture(db)
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
        )
        db.add(user)
        await db.flush()
        mapping = OCUserMapping(
            user_id=user.id,
            panel_id=panel.id,
            external_user_id=str(uuid.uuid4()),
            status=OC_MAPPING_STATUS_DELETED,
            last_synced_configs=[],
        )
        db.add(mapping)
        await db.commit()

        with patch("app.node.oc_sync.tenant_panel_allows_oc_user_mutations", return_value=True):
            await enqueue_oc_user_sync(db, user)
        await db.commit()

        jobs = (
            await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))
        ).scalars().all()
        assert jobs == []

        await db.delete(mapping)
        await db.delete(user)
        await db.commit()


@pytest.mark.asyncio
async def test_healthy_mapping_is_noop():
    async with GetDB() as db:
        _, admin, panel, group, _tag, cfg_id = await _panel86_fixture(db)
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        mapping = OCUserMapping(
            user_id=user.id,
            panel_id=panel.id,
            external_user_id=str(uuid.uuid4()),
            status=OC_MAPPING_STATUS_ACTIVE,
            last_synced_configs=[cfg_id],
            external_subscription_url="https://1.1.1.1/sub/ok",
        )
        db.add(mapping)
        await db.commit()

        with patch("app.node.oc_sync.tenant_panel_allows_oc_user_mutations", return_value=True):
            await enqueue_oc_user_sync(db, user)
        await db.commit()
        jobs = (
            await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))
        ).scalars().all()
        assert jobs == []

        await db.delete(mapping)
        await db.delete(user)
        await db.commit()


@pytest.mark.asyncio
async def test_stale_create_job_skipped_after_panel_removed():
    async with GetDB() as db:
        tenant = Tenant(name=f"t_{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.flush()
        intg = OCIntegration(base_url="http://test", api_token_encrypted="e", token_preview="test")
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="99",
            purchaser_identity="x",
            name="p",
            tenant_id=tenant.id,
            sync_status="connected",
        )
        user = User(username=f"u_{uuid.uuid4().hex[:6]}", status=UserStatus.active)
        db.add_all([panel, user])
        await db.flush()
        job = OCSyncState(
            entity_type="user_mapping",
            entity_id=f"{user.id}_{panel.id}",
            operation="create",
            idempotency_key=f"sync_{uuid.uuid4()}",
            payload={"groups": [], "configs": ["c1"]},
            status="pending",
        )
        db.add(job)
        await db.commit()
        job_id = job.id
        panel_id = panel.id
        await remove_imported_panel(db, panel)
        await db.commit()

        await db.execute(
            delete(OCSyncState).where(and_(OCSyncState.status == "pending", OCSyncState.id != job_id))
        )
        await db.commit()

        with patch("aiohttp.ClientSession.put", new=mock_put_with_sub), patch(
            "app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt
        ):
            await process_oc_sync()

        async with GetDB() as db_check:
            stale = (await db_check.execute(select(OCSyncState).where(OCSyncState.id == job_id))).scalar_one()
        assert stale.status == "completed"

        await db.execute(delete(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel_id}"))
        await db.delete(user)
        await db.delete(intg)
        await db.delete(tenant)
        await db.commit()


@pytest.mark.asyncio
async def test_reconcile_repairs_panel86_broken_mapping():
    async with GetDB() as db:
        _, admin, panel, group, _tag, _cfg = await _panel86_fixture(db)
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        ext = str(uuid.uuid4())
        mapping = OCUserMapping(
            user_id=user.id,
            panel_id=panel.id,
            external_user_id=ext,
            status=OC_MAPPING_STATUS_DELETED,
            last_synced_configs=[],
        )
        db.add(mapping)
        await db.commit()

        with patch("app.node.oc_sync.tenant_panel_allows_oc_user_mutations", return_value=True):
            user = (
                await db.execute(
                    select(User)
                    .where(User.id == user.id)
                    .options(selectinload(User.groups).selectinload(Group.inbounds))
                )
            ).scalar_one()
            await enqueue_oc_user_sync(db, user)
            await db.commit()
            job = (
                await db.execute(
                    select(OCSyncState).where(
                        OCSyncState.entity_id == f"{user.id}_{panel.id}",
                        OCSyncState.status == "pending",
                    )
                )
            ).scalar_one_or_none()
            assert job is not None
            m = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == mapping.id))).scalar_one()
            assert m.external_user_id == ext
            assert m.status == "pending"

        await db.execute(delete(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))
        await db.delete(mapping)
        await db.delete(user)
        await db.commit()
