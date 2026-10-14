"""Aggregated panel/workspace statistics (Phase 3-F).

Query assumptions:
- Per-panel user counts use enabled ``user_panel_bindings`` joined to ``users``.
- ``traffic_used`` sums ``users.used_traffic`` per binding; users on multiple panels
  contribute their full traffic to each panel row (documented approximation).
- ``last_activity_at`` is the max of user ``online_at`` and mapping ``last_synced_at``.
"""
from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, UserStatus
from app.db.models_oc import OCUserMapping, OCPanel, UserPanelBinding


async def count_panels_in_scope(
    db: AsyncSession,
    *,
    workspace_id: int | None,
    tenant_id: int | None,
    oc_account_id: int | None,
) -> int:
    stmt = select(func.count()).select_from(OCPanel)
    if workspace_id is not None:
        stmt = stmt.where(OCPanel.workspace_id == workspace_id)
    if tenant_id is not None:
        stmt = stmt.where(OCPanel.tenant_id == tenant_id)
    if oc_account_id is not None:
        stmt = stmt.where(OCPanel.oc_account_id == oc_account_id)
    return int((await db.execute(stmt)).scalar_one() or 0)


async def aggregate_panel_statistics(
    db: AsyncSession,
    *,
    workspace_id: int | None,
    tenant_id: int | None,
    oc_account_id: int | None,
) -> list[dict]:
    binding = UserPanelBinding
    user = User
    panel = OCPanel
    mapping = OCUserMapping

    panel_filter = []
    if workspace_id is not None:
        panel_filter.append(panel.workspace_id == workspace_id)
    if tenant_id is not None:
        panel_filter.append(panel.tenant_id == tenant_id)
    if oc_account_id is not None:
        panel_filter.append(panel.oc_account_id == oc_account_id)

    stmt = (
        select(
            panel.id.label("panel_id"),
            panel.name.label("panel_name"),
            panel.sync_status,
            panel.last_sync_at,
            func.count(func.distinct(user.id)).label("total_users"),
            func.count(func.distinct(case((user.status == UserStatus.active, user.id), else_=None))).label("active_users"),
            func.coalesce(func.sum(user.used_traffic), 0).label("traffic_used"),
            func.max(user.online_at).label("max_user_online"),
            func.max(mapping.last_synced_at).label("max_mapping_sync"),
            func.sum(case((user.status == UserStatus.active, 1), else_=0)).label("status_active"),
            func.sum(case((user.status == UserStatus.expired, 1), else_=0)).label("status_expired"),
            func.sum(case((user.status == UserStatus.disabled, 1), else_=0)).label("status_disabled"),
            func.sum(case((user.status == UserStatus.limited, 1), else_=0)).label("status_limited"),
            func.sum(case((user.status == UserStatus.on_hold, 1), else_=0)).label("status_on_hold"),
        )
        .select_from(panel)
        .outerjoin(binding, (binding.oc_panel_id == panel.id) & binding.enabled.is_(True))
        .outerjoin(user, user.id == binding.user_id)
        .outerjoin(mapping, (mapping.panel_id == panel.id) & (mapping.user_id == user.id))
        .where(*panel_filter)
        .group_by(panel.id, panel.name, panel.sync_status, panel.last_sync_at)
        .order_by(panel.id.asc())
    )

    rows = (await db.execute(stmt)).all()
    results: list[dict] = []
    for row in rows:
        last_candidates = [row.max_user_online, row.max_mapping_sync, row.last_sync_at]
        last_activity = None
        for candidate in last_candidates:
            if candidate is None:
                continue
            if last_activity is None or candidate > last_activity:
                last_activity = candidate
        results.append(
            {
                "panel_id": int(row.panel_id),
                "panel_name": row.panel_name,
                "sync_status": row.sync_status,
                "total_users": int(row.total_users or 0),
                "active_users": int(row.active_users or 0),
                "traffic_used": int(row.traffic_used or 0),
                "last_activity_at": last_activity,
                "expiration_distribution": {
                    "active": int(row.status_active or 0),
                    "expired": int(row.status_expired or 0),
                    "disabled": int(row.status_disabled or 0),
                    "limited": int(row.status_limited or 0),
                    "on_hold": int(row.status_on_hold or 0),
                },
            }
        )
    return results
