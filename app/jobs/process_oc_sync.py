import asyncio
import aiohttp
from datetime import UTC, datetime as dt

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from app import scheduler
from app.db import GetDB
from app.db.models_oc import OCIntegration, OCSyncState, OCPanel, OCUserMapping, TenantTelegramConnection
from app.services.oc_connection_credentials import (
    connection_token_by_id,
    connection_token_for_panel,
    connection_token_for_tenant,
)
from app.services.oc_integration_client import (
    OcConnectionTokenMissing,
    OcIntegrationApiError,
    _read_oc_error_detail,
)
from app.services.oc_connection_credentials import panel_has_active_connection
from app.db.models import User
from app.utils.crypto import decrypt_secret
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("oc-sync")

# OC answers with these when retrying cannot help; the job fails immediately and the
# 5-minute reconcile re-evaluates (a fresh job) once the underlying state changed.
_PERMANENT_OC_CODES = frozenset({
    "PANEL_INACTIVE",
    "CONNECTION_REVOKED",
    "CONNECTION_TOKEN_INVALID",
    "CONNECTION_TOKEN_REQUIRED",
    "ACCOUNT_MISMATCH",
    "ACCOUNT_BANNED",
    "PANEL_NOT_FOUND",
    "PANEL_CREDENTIALS_MISSING",
    "PANEL_TYPE_UNSUPPORTED",
    "GROUP_NOT_ALLOWED",
    "CONFIG_NOT_ALLOWED",
    "CONFIG_NOT_IN_GROUPS",
})
_PANEL_STATUS_BY_OC_CODE = {
    "PANEL_INACTIVE": "inactive",
    "CONNECTION_REVOKED": "connection_revoked",
}


async def _raise_for_oc(resp) -> None:
    """Raise OcIntegrationApiError (with OC's machine code in the detail) on HTTP >= 400."""
    if resp.status < 400:
        return
    try:
        detail = await _read_oc_error_detail(resp)
    except Exception:
        detail = f"HTTP {resp.status}"
    raise OcIntegrationApiError(oc_status=resp.status, detail=detail)


def _oc_headers(token: str, connection_token: str | None) -> dict[str, str]:
    headers = {
        "X-Integration-Token": token,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if connection_token:
        headers["X-OC-Connection-Token"] = connection_token
    return headers


def _subscription_url_from(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    url = str(payload.get("subscription_url") or "").strip()
    return url if url.lower().startswith(("http://", "https://")) and len(url) <= 2048 else None


async def _process_connection_revoke(db, client: aiohttp.ClientSession, job: OCSyncState) -> None:
    """Propagate a local Telegram-connection revoke to Outbound Center (idempotent on OC)."""
    payload = job.payload or {}
    connection_id = int(payload["connection_id"])
    integration = (
        await db.execute(select(OCIntegration).where(OCIntegration.id == int(payload["integration_id"])))
    ).scalar_one_or_none()
    if integration is None:
        raise ValueError(f"Integration {payload.get('integration_id')} not found")
    connection_token = await connection_token_by_id(db, connection_id)
    token = await decrypt_secret(integration.api_token_encrypted)
    url = f"{integration.base_url.rstrip('/')}/v1/integration/pasarguard/connection/revoke"
    async with client.post(url, headers=_oc_headers(token, connection_token)) as resp:
        await _raise_for_oc(resp)


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
                panel = None
                try:
                    original_revision = job.revision

                    if job.entity_type == "oc_connection":
                        await _process_connection_revoke(db, client, job)
                        result = await db.execute(
                            update(OCSyncState)
                            .where(OCSyncState.id == job.id, OCSyncState.revision == original_revision)
                            .values(status="completed")
                        )
                        await db.commit()
                        continue

                    if job.entity_type == "panel_discovery_user":
                        original_payload = job.payload.copy() if job.payload else {}
                        external_user_id = original_payload.get("external_user_id")
                        if not external_user_id:
                            raise ValueError("external_user_id missing in discovery delete payload")
                        integration_id = original_payload.get("integration_id")
                        source_panel_id = original_payload.get("source_panel_id")
                        if not integration_id or not source_panel_id:
                            raise ValueError(
                                "Discovery delete job missing integration_id or source_panel_id snapshot"
                            )
                        integration = (
                            await db.execute(
                                select(OCIntegration).where(OCIntegration.id == int(integration_id))
                            )
                        ).scalar_one_or_none()
                        if not integration or not integration.is_active:
                            raise ValueError(f"Integration {integration_id} not found or disabled")
                        if original_payload.get("connection_id"):
                            connection_token = await connection_token_by_id(
                                db, int(original_payload["connection_id"])
                            )
                        else:
                            connection_token = await connection_token_for_tenant(
                                db,
                                original_payload.get("tenant_id"),
                                oc_account_id=original_payload.get("oc_account_id"),
                                allow_revoked=True,
                            )
                        token = await decrypt_secret(integration.api_token_encrypted)
                        headers = _oc_headers(token, connection_token)
                        url = (
                            f"{integration.base_url.rstrip('/')}/v1/integration/panels/"
                            f"{source_panel_id}/users/{external_user_id}"
                        )
                        async with client.delete(url, headers=headers) as resp:
                            if resp.status != 404:
                                await _raise_for_oc(resp)
                        stmt = (
                            update(OCSyncState)
                            .where(OCSyncState.id == job.id, OCSyncState.revision == original_revision)
                            .values(status="completed")
                        )
                        await db.execute(stmt)
                        await db.commit()
                        continue

                    parts = job.entity_id.split("_")
                    if len(parts) != 2:
                        raise ValueError(f"Invalid entity_id: {job.entity_id}")
                    
                    user_id = int(parts[0])
                    panel_id = int(parts[1])

                    # Snapshot original intent
                    original_payload = job.payload.copy() if job.payload else {}
                    original_operation = job.operation

                    panel = (await db.execute(
                        select(OCPanel)
                        .options(selectinload(OCPanel.integration))
                        .where(OCPanel.id == panel_id)
                    )).scalar_one_or_none()

                    if job.operation in ("create", "update"):
                        if not panel or not panel.integration.is_active:
                            raise ValueError(f"Panel {panel_id} not found or integration disabled")
                        db_user = await db.get(User, user_id)
                        if db_user is None:
                            raise ValueError(f"User {user_id} not found for sync job {job.id}")
                        if (
                            panel.workspace_id is not None
                            and db_user.workspace_id is not None
                            and db_user.workspace_id != panel.workspace_id
                        ):
                            raise ValueError(
                                f"Workspace mismatch for user {user_id} and panel {panel_id}"
                            )
                        if panel.tenant_id is not None and not await panel_has_active_connection(db, panel):
                            stmt = (
                                update(OCSyncState)
                                .where(OCSyncState.id == job.id, OCSyncState.revision == original_revision)
                                .values(status="completed")
                            )
                            await db.execute(stmt)
                            await db.commit()
                            continue

                    if job.operation in ("create", "update"):
                        token = await decrypt_secret(panel.integration.api_token_encrypted)
                        connection_token = await connection_token_for_panel(db, panel)
                        headers = _oc_headers(token, connection_token)
                        mapping = (await db.execute(
                            select(OCUserMapping)
                            .where(OCUserMapping.user_id == user_id, OCUserMapping.panel_id == panel_id)
                        )).scalar_one_or_none()

                        if not mapping:
                            raise ValueError(f"UserMapping not found for {job.entity_id}")

                        external_user_id = mapping.external_user_id
                        url = f"{panel.integration.base_url.rstrip('/')}/v1/integration/panels/{panel.source_panel_id}/users/{external_user_id}"

                        groups = list(original_payload.get("groups") or [])
                        configs = list(original_payload.get("configs") or [])
                        put_bodies: list[dict] = []
                        if groups:
                            put_bodies.append({"groups": groups, "configs": []})
                        put_bodies.append({"groups": groups, "configs": configs})

                        subscription_url = None
                        last_put_err: OcIntegrationApiError | None = None
                        for idx, body in enumerate(put_bodies):
                            async with client.put(url, headers=headers, json=body) as resp:
                                try:
                                    await _raise_for_oc(resp)
                                except OcIntegrationApiError as exc:
                                    last_put_err = exc
                                    retryable = exc.code in ("CONFIG_NOT_ALLOWED", "CONFIG_NOT_IN_GROUPS")
                                    if retryable and idx < len(put_bodies) - 1:
                                        continue
                                    raise
                                try:
                                    subscription_url = _subscription_url_from(await resp.json())
                                except Exception:
                                    subscription_url = None
                                break
                        else:
                            if last_put_err:
                                raise last_put_err
                            raise ValueError("OC user PUT produced no successful body")

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
                        if subscription_url:
                            mapping.external_subscription_url = subscription_url
                        if panel.sync_status in ("inactive", "connection_revoked"):
                            panel.sync_status = "connected"  # OC accepted the write: it is usable again

                    elif job.operation == "delete":
                        external_user_id = original_payload.get("external_user_id")
                        if not external_user_id:
                            raise ValueError("external_user_id missing in payload")

                        if panel and panel.integration.is_active:
                            integration = panel.integration
                            source_panel_id = panel.source_panel_id
                            tenant_id = panel.tenant_id
                            oc_account_id = panel.oc_account_id
                        else:
                            integration_id = original_payload.get("integration_id")
                            source_panel_id = original_payload.get("source_panel_id")
                            if not integration_id or not source_panel_id:
                                raise ValueError(
                                    "Panel removed; delete job missing integration_id or source_panel_id snapshot"
                                )
                            integration = (
                                await db.execute(
                                    select(OCIntegration).where(OCIntegration.id == int(integration_id))
                                )
                            ).scalar_one_or_none()
                            if not integration or not integration.is_active:
                                raise ValueError(f"Integration {integration_id} not found or disabled")
                            tenant_id = original_payload.get("tenant_id")
                            oc_account_id = original_payload.get("oc_account_id")

                        # Deletes stay authorised after a local revoke: use the connection
                        # snapshotted in the job, else the tenant's newest token-bearing one.
                        if original_payload.get("connection_id"):
                            connection_token = await connection_token_by_id(
                                db, int(original_payload["connection_id"])
                            )
                        else:
                            connection_token = await connection_token_for_tenant(
                                db, tenant_id, oc_account_id=oc_account_id, allow_revoked=True
                            )

                        token = await decrypt_secret(integration.api_token_encrypted)
                        headers = _oc_headers(token, connection_token)

                        url = f"{integration.base_url.rstrip('/')}/v1/integration/panels/{source_panel_id}/users/{external_user_id}"
                        
                        async with client.delete(url, headers=headers) as resp:
                            # OC DELETE is idempotent (204 even if absent); 404 means the
                            # panel is no longer reachable for this account -> nothing to delete.
                            if resp.status != 404:
                                await _raise_for_oc(resp)

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
                            mapping.external_subscription_url = None
                
                except Exception as e:
                    logger.error(f"Failed to process OCSyncState {job.id}: {e}")
                    job.attempts += 1
                    job.last_error = str(e)[:2000]
                    permanent = isinstance(e, OcConnectionTokenMissing) or (
                        isinstance(e, OcIntegrationApiError)
                        and (e.code in _PERMANENT_OC_CODES)
                    )
                    if isinstance(e, OcIntegrationApiError) and panel is not None:
                        new_status = _PANEL_STATUS_BY_OC_CODE.get(e.code or "")
                        if new_status and job.operation in ("create", "update"):
                            # Reflect OC's answer locally so we stop re-queueing doomed writes.
                            panel.sync_status = new_status
                    if permanent or job.attempts >= job.max_attempts:
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
