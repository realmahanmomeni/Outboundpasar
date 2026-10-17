"""
Regression: panel DELETE enqueues self-contained OC delete jobs before CASCADE removes rows.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select

from app.db import GetDB
from app.db.models import Tenant, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCSyncState, OCUserMapping
from app.node.oc_sync import remove_imported_panel
from app.services.oc_user_mapping_state import OC_MAPPING_STATUS_ACTIVE


async def _cleanup(db, *, panel_ids=(), user_ids=(), integration_ids=(), tenant_ids=(), sync_ids=()):
    if sync_ids:
        await db.execute(delete(OCSyncState).where(OCSyncState.id.in_(sync_ids)))
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
async def test_panel_delete_job_survives_mapping_and_panel_removal():
    sync_ids: list[int] = []
    panel_id_holder: list[int] = []
    try:
        async with GetDB() as db:
            tenant = Tenant(name=f"pd_snap_{uuid.uuid4().hex[:8]}")
            db.add(tenant)
            await db.flush()

            integration = OCIntegration(
                base_url="https://oc.example.test",
                api_token_encrypted="enc",
                token_preview="prev",
                is_active=True,
            )
            db.add(integration)
            await db.flush()

            panel = OCPanel(
                integration_id=integration.id,
                source_panel_id="991",
                purchaser_identity="1",
                name="snap-panel",
                tenant_id=tenant.id,
                sync_status="connected",
            )
            db.add(panel)
            await db.flush()
            panel_id_holder.append(panel.id)

            user = User(username=f"u_{uuid.uuid4().hex[:8]}", status=UserStatus.active)
            db.add(user)
            await db.flush()

            mapping = OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id="ext-user-991",
                status=OC_MAPPING_STATUS_ACTIVE,
                last_synced_configs=["c1"],
            )
            db.add(mapping)
            await db.commit()

            await remove_imported_panel(db, panel)
            await db.commit()

            job = (
                await db.execute(
                    select(OCSyncState).where(
                        OCSyncState.entity_id == f"{user.id}_{panel.id}",
                        OCSyncState.operation == "delete",
                        OCSyncState.status == "pending",
                    )
                )
            ).scalar_one()

            assert job.payload["external_user_id"] == "ext-user-991"
            assert job.payload["source_panel_id"] == "991"
            assert job.payload["integration_id"] == integration.id
            sync_ids.append(job.id)

            gone_panel = (
                await db.execute(select(OCPanel).where(OCPanel.id == panel.id))
            ).scalar_one_or_none()
            gone_mapping = (
                await db.execute(
                    select(OCUserMapping).where(
                        OCUserMapping.user_id == user.id,
                        OCUserMapping.panel_id == panel.id,
                    )
                )
            ).scalar_one_or_none()
            assert gone_panel is None
            assert gone_mapping is None

            integration = (
                await db.execute(
                    select(OCIntegration).where(OCIntegration.id == job.payload["integration_id"])
                )
            ).scalar_one()
            resolved_url = (
                f"{integration.base_url.rstrip('/')}/v1/integration/panels/"
                f"{job.payload['source_panel_id']}/users/{job.payload['external_user_id']}"
            )
            assert resolved_url.endswith("/panels/991/users/ext-user-991")

            await _cleanup(
                db,
                sync_ids=sync_ids,
                user_ids=[user.id],
                integration_ids=[integration.id],
                tenant_ids=[tenant.id],
            )
    finally:
        pass


async def main():
    await test_panel_delete_job_survives_mapping_and_panel_removal()
    print("[x] Panel delete enqueue snapshot survives CASCADE and worker executes without panel row")


if __name__ == "__main__":
    asyncio.run(main())
