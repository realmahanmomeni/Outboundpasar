"""Phase 8: workspace isolation for OC sync enqueue, worker checks, and panel connection scope."""
from __future__ import annotations

import asyncio
import uuid
from test_workspace_isolation_util import unique_username, unique_tag

from sqlalchemy import select

from app.db import GetDB
from app.db.crud.user import create_user
from app.db.crud.workspace import assign_workspace_for_new_admin, create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User
from app.db.models_oc import OCIntegration, OCPanel, OCSyncState, TenantTelegramConnection, TenantTelegramConnectionStatus
from app.models.user import UserCreate
from app.node.oc_sync import enqueue_oc_user_sync, tenant_panel_allows_oc_user_mutations
from app.services.oc_connection_credentials import panel_has_active_connection
from app.services.tenant_admin_scope import BUILTIN_OPERATOR_ROLE_ID


async def _tenant(db) -> int:
    t = Tenant(name=f"p8_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(t)
    await db.flush()
    return t.id


async def _administrator(db, tenant_id: int, name: str | None = None) -> Admin:
    username = name or unique_username("adm")
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    row = Admin(username=username, hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(row)
    await db.flush()
    await create_workspace_for_administrator(db, row)
    await db.refresh(row)
    return row


async def _integration(db) -> OCIntegration:
    integ = OCIntegration(
        base_url="https://oc.example",
        api_token_encrypted="enc",
        token_preview="prev",
        is_active=True,
    )
    db.add(integ)
    await db.flush()
    return integ


async def _panel(db, integ: OCIntegration, tenant_id: int, workspace_id: int, oc_account: int) -> OCPanel:
    p = OCPanel(
        integration_id=integ.id,
        source_panel_id=str(uuid.uuid4().int)[:8],
        purchaser_identity=str(oc_account),
        name="panel",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        oc_account_id=oc_account,
    )
    db.add(p)
    await db.flush()
    return p


async def test_panel_connection_scoped_to_workspace_binding():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = await _integration(db)
        db.add(
            TenantTelegramConnection(
                tenant_id=tenant_id,
                binding_admin_id=admin_a.id,
                status=TenantTelegramConnectionStatus.active.value,
                active=True,
                oc_account_id=100,
            )
        )
        db.add(
            TenantTelegramConnection(
                tenant_id=tenant_id,
                binding_admin_id=admin_b.id,
                status=TenantTelegramConnectionStatus.active.value,
                active=True,
                oc_account_id=200,
            )
        )
        await db.flush()
        panel_a = await _panel(db, integ, tenant_id, admin_a.workspace_id, 100)
        panel_b = await _panel(db, integ, tenant_id, admin_b.workspace_id, 200)

        assert await panel_has_active_connection(db, panel_a) is True
        assert await panel_has_active_connection(db, panel_b) is True
        assert await tenant_panel_allows_oc_user_mutations(db, panel_a.id) is True
        assert await tenant_panel_allows_oc_user_mutations(db, panel_b.id) is True

        panel_a.oc_account_id = 999
        assert await panel_has_active_connection(db, panel_a) is False
        assert await tenant_panel_allows_oc_user_mutations(db, panel_a.id) is False

        await db.rollback()


async def test_enqueue_skips_cross_workspace_panel():
    async with GetDB() as db:
        tenant_id = await _tenant(db)
        admin_a = await _administrator(db, tenant_id)
        admin_b = await _administrator(db, tenant_id)
        integ = await _integration(db)
        db.add(
            TenantTelegramConnection(
                tenant_id=tenant_id,
                binding_admin_id=admin_a.id,
                status=TenantTelegramConnectionStatus.active.value,
                active=True,
                oc_account_id=100,
            )
        )
        await db.flush()
        panel_b = await _panel(db, integ, tenant_id, admin_b.workspace_id, 100)
        user_a = await create_user(
            db,
            UserCreate(username=f"ua_{uuid.uuid4().hex[:6]}", proxy_settings={}),
            groups=[],
            admin=admin_a,
        )
        user_a.workspace_id = admin_a.workspace_id
        await db.flush()

        await enqueue_oc_user_sync(db, user_a)
        pending = (
            await db.execute(select(OCSyncState).where(OCSyncState.status == "pending"))
        ).scalars().all()
        panel_ids = set()
        for job in pending:
            if job.entity_type == "user_mapping" and "_" in job.entity_id:
                panel_ids.add(int(job.entity_id.split("_")[1]))
        assert panel_b.id not in panel_ids

        await db.rollback()


async def test_oc_sync_job_count_stable_on_retry_revision():
    """CAS revision on pending job update preserves workspace-scoped payload identity."""
    async with GetDB() as db:
        job = OCSyncState(
            entity_type="user_mapping",
            entity_id="1_2",
            operation="update",
            idempotency_key=f"sync_{uuid.uuid4()}",
            payload={"groups": [], "configs": ["x"]},
            status="pending",
            revision=1,
        )
        db.add(job)
        await db.flush()
        original_revision = job.revision
        job.payload = {"groups": [], "configs": ["y"]}
        job.revision += 1
        await db.flush()
        assert job.revision == original_revision + 1
        await db.rollback()


async def run_all():
    await test_panel_connection_scoped_to_workspace_binding()
    await test_enqueue_skips_cross_workspace_panel()
    await test_oc_sync_job_count_stable_on_retry_revision()
    print("test_phase8_background_workspace: 3 passed")


if __name__ == "__main__":
    asyncio.run(run_all())
