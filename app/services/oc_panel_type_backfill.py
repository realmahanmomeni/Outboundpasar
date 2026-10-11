"""Bulk backfill OCPanel.panel_type from Outbound Center account panel list.

Outbound Center requires ``X-OC-Connection-Token`` (issued via Telegram linking) for
``GET /v1/integration/accounts/{account_id}/panels``. There is no separate metadata-only
credential: integration ``api_token`` alone cannot scope buyer panels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel, TenantTelegramConnection
from app.services.oc_connection_credentials import get_connection_for_tenant
from app.services.oc_integration_client import (
    OcConnectionTokenMissing,
    OcIntegrationApiError,
    call_oc_api,
    get_active_integration,
)
from app.services.oc_panel_buyer_auth import normalize_panel_type
from app.services.oc_panel_import_metadata import apply_panel_metadata_from_oc

logger = logging.getLogger(__name__)


@dataclass
class PanelTypeBackfillReport:
    null_before: int = 0
    null_after: int = 0
    tenants_seen: int = 0
    tenants_with_token: int = 0
    tenants_skipped_no_token: list[int] = field(default_factory=list)
    panels_updated: int = 0
    panels_unchanged: int = 0
    panels_no_oc_match: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "null_before": self.null_before,
            "null_after": self.null_after,
            "tenants_seen": self.tenants_seen,
            "tenants_with_token": self.tenants_with_token,
            "tenants_skipped_no_token": self.tenants_skipped_no_token,
            "panels_updated": self.panels_updated,
            "panels_unchanged": self.panels_unchanged,
            "panels_no_oc_match": self.panels_no_oc_match,
            "errors": self.errors,
        }


async def _account_panel_type_map(
    db: AsyncSession,
    integration,
    tenant_id: int,
    oc_account_id: int,
    *,
    allow_revoked: bool,
) -> dict[str, str] | None:
    """subscription_id -> normalized panel_type, or None when no connection token."""
    row = await get_connection_for_tenant(
        db, tenant_id, oc_account_id=oc_account_id, allow_revoked=allow_revoked
    )
    if row is None or not row.oc_connection_token_encrypted:
        return None
    from app.services.oc_connection_credentials import connection_token_by_id

    token = await connection_token_by_id(db, row.id)
    try:
        data = await call_oc_api(
            integration,
            "GET",
            f"/v1/integration/accounts/{oc_account_id}/panels",
            connection_token=token,
        )
    except (OcIntegrationApiError, OcConnectionTokenMissing) as exc:
        logger.info(
            "panel_type_backfill_fetch_failed tenant=%s account=%s err=%s",
            tenant_id,
            oc_account_id,
            type(exc).__name__,
        )
        return None
    out: dict[str, str] = {}
    for item in data.get("items") or []:
        sid = str(item.get("subscription_id") or item.get("id") or "").strip()
        ptype = normalize_panel_type(str(item.get("panel_type") or ""))
        if sid and ptype:
            out[sid] = ptype
    return out


async def backfill_null_panel_types(
    db: AsyncSession,
    *,
    tenant_id: int | None = None,
    dry_run: bool = True,
    allow_revoked: bool = True,
) -> PanelTypeBackfillReport:
    """
    Fill ``panel_type`` only where NULL, using OC account panel list.

    Never overwrites a non-empty ``panel_type``.
    """
    report = PanelTypeBackfillReport()
    report.null_before = int(
        await db.scalar(
            select(func.count())
            .select_from(OCPanel)
            .where(OCPanel.panel_type.is_(None))
            .where(OCPanel.tenant_id == tenant_id if tenant_id is not None else True)
        )
        or 0
    )

    integration = await get_active_integration(db)
    if integration is None:
        report.errors.append("No active OC integration configured")
        report.null_after = report.null_before
        return report

    stmt = (
        select(OCPanel.tenant_id, OCPanel.oc_account_id)
        .where(OCPanel.panel_type.is_(None))
        .where(OCPanel.tenant_id.isnot(None))
        .where(OCPanel.oc_account_id.isnot(None))
        .distinct()
    )
    if tenant_id is not None:
        stmt = stmt.where(OCPanel.tenant_id == tenant_id)
    tenant_accounts = (await db.execute(stmt)).all()
    report.tenants_seen = len(tenant_accounts)

    type_by_subscription: dict[tuple[int, int], dict[str, str]] = {}
    for tid, account_id in tenant_accounts:
        if tid is None or account_id is None:
            continue
        key = (tid, account_id)
        if key not in type_by_subscription:
            fetched = await _account_panel_type_map(
                db, integration, tid, account_id, allow_revoked=allow_revoked
            )
            if fetched is None:
                if tid not in report.tenants_skipped_no_token:
                    report.tenants_skipped_no_token.append(tid)
                type_by_subscription[key] = {}
            else:
                report.tenants_with_token += 1
                type_by_subscription[key] = fetched
        cached = type_by_subscription[key]

        panels = (
            await db.execute(
                select(OCPanel).where(
                    OCPanel.tenant_id == tid,
                    OCPanel.oc_account_id == account_id,
                    OCPanel.panel_type.is_(None),
                )
            )
        ).scalars().all()
        for panel in panels:
            sid = str(panel.source_panel_id).strip()
            ptype = cached.get(sid)
            if not ptype:
                report.panels_no_oc_match += 1
                continue
            report.panels_updated += 1
            if not dry_run:
                panel.panel_type = ptype
                meta = {"panel_type": ptype}
                await apply_panel_metadata_from_oc(db, panel, meta)

    if not dry_run and report.panels_updated:
        await db.commit()

    report.null_after = int(
        await db.scalar(
            select(func.count())
            .select_from(OCPanel)
            .where(OCPanel.panel_type.is_(None))
            .where(OCPanel.tenant_id == tenant_id if tenant_id is not None else True)
        )
        or 0
    )
    report.panels_unchanged = report.null_before - report.null_after - (
        report.panels_updated if dry_run else 0
    )
    return report


async def classify_null_panel_types(db: AsyncSession) -> dict[str, Any]:
    """Summarize NULL panel_type rows without mutating data."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.orm import selectinload

    from app.db.models_oc import OCPanelConfig, OCPanelGroup, OCUserMapping
    from app.services.oc_test_integration import is_test_oc_integration_base_url

    rows = (
        await db.execute(
            select(OCPanel)
            .options(selectinload(OCPanel.integration))
            .where(OCPanel.panel_type.is_(None))
        )
    ).scalars().all()
    summary: dict[str, int] = {
        "total_null": len(rows),
        "test_integration_artifact": 0,
        "safe_orphan_no_refs": 0,
        "legitimate_unassigned_no_tenant": 0,
        "migration_artifact_no_tenant_with_refs": 0,
        "blocked_no_connection_token": 0,
        "refreshable_has_token": 0,
        "likely_test_or_wizard": 0,
        "production_unclassified": 0,
        "unknown": 0,
    }
    recent_cutoff = datetime.now(UTC) - timedelta(hours=48)
    for panel in rows:
        integ_url = panel.integration.base_url if panel.integration else ""
        if is_test_oc_integration_base_url(integ_url):
            summary["test_integration_artifact"] += 1
            continue

        cfg_n = int(
            await db.scalar(
                select(func.count()).select_from(OCPanelConfig).where(OCPanelConfig.panel_id == panel.id)
            )
            or 0
        )
        grp_n = int(
            await db.scalar(
                select(func.count()).select_from(OCPanelGroup).where(OCPanelGroup.panel_id == panel.id)
            )
            or 0
        )
        map_n = int(
            await db.scalar(
                select(func.count()).select_from(OCUserMapping).where(OCUserMapping.panel_id == panel.id)
            )
            or 0
        )
        has_refs = bool(cfg_n or grp_n or map_n)

        name_l = (panel.name or "").lower()
        is_testish = any(
            x in name_l for x in ("test", "p15", "phase", "idor", "wizard", "mp_", "sync_test")
        ) or (panel.sync_status or "") == "pending"

        if panel.tenant_id is None:
            if not has_refs:
                summary["safe_orphan_no_refs"] += 1
            elif is_testish:
                summary["migration_artifact_no_tenant_with_refs"] += 1
            else:
                summary["legitimate_unassigned_no_tenant"] += 1
            continue

        conn = await get_connection_for_tenant(
            db, panel.tenant_id, oc_account_id=panel.oc_account_id, allow_revoked=True
        )
        has_token = conn is not None and bool(conn.oc_connection_token_encrypted)
        if not has_token:
            summary["blocked_no_connection_token"] += 1
            if is_testish:
                summary["likely_test_or_wizard"] += 1
            continue
        summary["refreshable_has_token"] += 1
        created = getattr(panel, "created_at", None)
        if is_testish or (created and created >= recent_cutoff):
            summary["likely_test_or_wizard"] += 1
        elif has_refs:
            summary["production_unclassified"] += 1
        else:
            summary["unknown"] += 1
    summary["null_before"] = summary["total_null"]
    return summary


async def panels_missing_metadata_diagnostic(db: AsyncSession) -> list[dict[str, Any]]:
    """Panels with NULL panel_type and why refresh may be unavailable."""
    rows = (
        await db.execute(
            select(OCPanel)
            .where(OCPanel.panel_type.is_(None))
            .order_by(OCPanel.tenant_id.nulls_last(), OCPanel.id)
            .limit(500)
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for panel in rows:
        reason = "unknown"
        if panel.tenant_id is None:
            reason = "no_tenant"
        else:
            row = await get_connection_for_tenant(
                db, panel.tenant_id, oc_account_id=panel.oc_account_id, allow_revoked=True
            )
            if row is None or not row.oc_connection_token_encrypted:
                reason = "no_connection_token"
            else:
                reason = "refreshable_via_account_panels"
        out.append(
            {
                "panel_id": panel.id,
                "tenant_id": panel.tenant_id,
                "source_panel_id": panel.source_panel_id,
                "sync_status": panel.sync_status,
                "reason": reason,
            }
        )
    return out
