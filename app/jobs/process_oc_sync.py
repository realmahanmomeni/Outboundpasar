import asyncio
import aiohttp
from datetime import UTC, datetime as dt

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from app import scheduler
from app.db import GetDB
from app.db.models_oc import OCSyncState, OCPanel, OCUserMapping
from app.utils.crypto import decrypt_secret
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("oc-sync")

async def process_oc_sync():
    async with GetDB() as db:
        # Fetch up to 50 pending jobs
        jobs = (await db.execute(
            select(OCSyncState)
            .where(OCSyncState.status == "pending")
            .order_by(OCSyncState.id.asc())
            .limit(50)
        )).scalars().all()

        if not jobs:
            return

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10.0)) as client:
            for job in jobs:
                try:
                    parts = job.entity_id.split("_")
                    if len(parts) != 2:
                        raise ValueError(f"Invalid entity_id: {job.entity_id}")
                    
                    user_id = int(parts[0])
                    panel_id = int(parts[1])

                    panel = (await db.execute(
                        select(OCPanel)
                        .options(selectinload(OCPanel.integration))
                        .where(OCPanel.id == panel_id)
                    )).scalar_one_or_none()

                    if not panel or not panel.integration.is_active:
                        raise ValueError(f"Panel {panel_id} not found or integration disabled")

                    token = await decrypt_secret(panel.integration.api_token_encrypted)
                    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                    
                    # Snapshot original intent
                    original_payload = job.payload.copy() if job.payload else None
                    original_operation = job.operation
                    original_revision = job.revision

                    if job.operation in ("create", "update"):
                        mapping = (await db.execute(
                            select(OCUserMapping)
                            .where(OCUserMapping.user_id == user_id, OCUserMapping.panel_id == panel_id)
                        )).scalar_one_or_none()

                        if not mapping:
                            raise ValueError(f"UserMapping not found for {job.entity_id}")

                        external_user_id = mapping.external_user_id
                        url = f"{panel.integration.base_url.rstrip('/')}/v1/integration/panels/{panel.source_panel_id}/users/{external_user_id}"
                        
                        async with client.put(url, headers=headers, json={
                            "groups": original_payload.get("groups", []),
                            "configs": original_payload.get("configs", [])
                        }) as resp:
                            resp.raise_for_status()

                        # CAS Update to protect against stale execution
                        stmt = (
                            update(OCSyncState)
                            .where(OCSyncState.id == job.id, OCSyncState.revision == original_revision)
                            .values(status="completed")
                        )
                        result = await db.execute(stmt)

                        if result.rowcount == 0:
                            logger.warning(f"Job {job.id} modified during execution. Discarding stale result.")
                            continue

                        # Phase 10: update success state
                        mapping.last_synced_configs = original_payload.get("configs", [])
                        mapping.status = "active"
                        mapping.last_synced_at = dt.now(UTC)

                    elif job.operation == "delete":
                        external_user_id = original_payload.get("external_user_id")
                        if not external_user_id:
                            raise ValueError("external_user_id missing in payload")

                        url = f"{panel.integration.base_url.rstrip('/')}/v1/integration/panels/{panel.source_panel_id}/users/{external_user_id}"
                        
                        async with client.delete(url, headers=headers) as resp:
                            # Gracefully handle 404 since it means already deleted
                            if resp.status != 404:
                                resp.raise_for_status()

                        # CAS Update to protect against stale execution
                        stmt = (
                            update(OCSyncState)
                            .where(OCSyncState.id == job.id, OCSyncState.revision == original_revision)
                            .values(status="completed")
                        )
                        result = await db.execute(stmt)

                        if result.rowcount == 0:
                            logger.warning(f"Job {job.id} modified during execution. Discarding stale result.")
                            continue

                        # Phase 10: update success state for delete
                        mapping = (await db.execute(
                            select(OCUserMapping)
                            .where(OCUserMapping.user_id == user_id, OCUserMapping.panel_id == panel_id)
                        )).scalar_one_or_none()

                        if mapping:
                            mapping.last_synced_configs = []
                            mapping.status = "deleted"
                            mapping.last_synced_at = dt.now(UTC)
                
                except Exception as e:
                    logger.error(f"Failed to process OCSyncState {job.id}: {e}")
                    job.attempts += 1
                    job.last_error = str(e)[:2000]
                    if job.attempts >= job.max_attempts:
                        job.status = "failed"
                
                await db.commit()

if runtime_settings.role.runs_scheduler:
    # Run frequently since it drains 50 items at a time
    scheduler.add_job(
        process_oc_sync,
        "interval",
        coalesce=True,
        seconds=10,
        max_instances=1,
        id="process_oc_sync",
        replace_existing=True,
    )
