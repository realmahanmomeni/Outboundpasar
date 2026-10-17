"""Idempotent remediation: panel workspace_id and dependent group/host backfill.

Revision ID: d4e5f6a7b8c4
Revises: c3d4e5f6a7b3

Re-runs deterministic Phase 1 panel workspace assignment when prerequisites exist,
then re-applies Phase 3 group/host backfills that depend on panel.workspace_id.
No weak heuristics; ambiguous rows remain NULL.
"""
from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text

revision = "d4e5f6a7b8c4"
down_revision = "c3d4e5f6a7b3"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()
    panels_assigned, panels_ambiguous = _remediate_panel_workspaces(conn)
    groups_remediated = _remediate_group_workspaces(conn)
    hosts_remediated = _remediate_host_workspaces_from_panels(conn)
    _log_stats(conn, panels_assigned, panels_ambiguous, groups_remediated, hosts_remediated)


def _remediate_panel_workspaces(conn) -> tuple[int, int]:
    """Same rules as e8f9a0b1c2d3 Phase 1 panel backfill."""
    panels_assigned = 0
    panels_ambiguous = 0
    panels = conn.execute(
        text("SELECT id, tenant_id, oc_account_id FROM oc_panels WHERE workspace_id IS NULL")
    ).fetchall()
    for panel_id, tenant_id, oc_account_id in panels:
        if tenant_id is None or oc_account_id is None:
            panels_ambiguous += 1
            continue
        bindings = conn.execute(
            text(
                "SELECT DISTINCT binding_admin_id FROM tenant_telegram_connections "
                "WHERE tenant_id = :tid AND oc_account_id = :oc AND binding_admin_id IS NOT NULL"
            ),
            {"tid": tenant_id, "oc": oc_account_id},
        ).fetchall()
        admin_ids = [row[0] for row in bindings if row[0] is not None]
        if len(admin_ids) != 1:
            panels_ambiguous += 1
            continue
        ws_id = conn.execute(
            text("SELECT workspace_id FROM admins WHERE id = :aid"),
            {"aid": admin_ids[0]},
        ).scalar_one_or_none()
        if ws_id is None:
            panels_ambiguous += 1
            continue
        conn.execute(
            text("UPDATE oc_panels SET workspace_id = :wid WHERE id = :pid AND workspace_id IS NULL"),
            {"wid": ws_id, "pid": panel_id},
        )
        panels_assigned += 1
    return panels_assigned, panels_ambiguous


def _remediate_group_workspaces(conn) -> int:
    result = conn.execute(
        text(
            """
            UPDATE groups AS g
            SET workspace_id = sub.ws
            FROM (
                SELECT uga.groups_id AS group_id, MIN(u.workspace_id) AS ws
                FROM users_groups_association AS uga
                JOIN users AS u ON u.id = uga.user_id
                WHERE u.workspace_id IS NOT NULL
                GROUP BY uga.groups_id
                HAVING COUNT(DISTINCT u.workspace_id) = 1
            ) AS sub
            WHERE g.id = sub.group_id
              AND g.workspace_id IS NULL
            """
        )
    )
    return result.rowcount or 0


def _remediate_host_workspaces_from_panels(conn) -> int:
    result = conn.execute(
        text(
            """
            UPDATE hosts AS h
            SET workspace_id = p.workspace_id,
                tenant_id = COALESCE(h.tenant_id, p.tenant_id)
            FROM oc_panel_destination_hosts AS d
            JOIN oc_panels AS p ON p.id = d.panel_id
            WHERE h.inbound_tag = d.virtual_inbound_tag
              AND h.workspace_id IS NULL
              AND p.workspace_id IS NOT NULL
              AND d.virtual_inbound_tag IS NOT NULL
            """
        )
    )
    return result.rowcount or 0


def _log_stats(
    conn,
    panels_assigned: int,
    panels_ambiguous: int,
    groups_remediated: int,
    hosts_remediated: int,
) -> None:
    logger.info(
        "Phase3 remediation: panels_assigned=%s panels_still_ambiguous=%s "
        "groups_remediated=%s hosts_remediated=%s "
        "groups_total=%s groups_with_workspace=%s groups_without_workspace=%s "
        "hosts_total=%s hosts_with_workspace=%s hosts_without_workspace=%s "
        "panels_total=%s panels_with_workspace=%s panels_without_workspace=%s",
        panels_assigned,
        panels_ambiguous,
        groups_remediated,
        hosts_remediated,
        conn.execute(text("SELECT COUNT(*) FROM groups")).scalar_one(),
        conn.execute(text("SELECT COUNT(*) FROM groups WHERE workspace_id IS NOT NULL")).scalar_one(),
        conn.execute(text("SELECT COUNT(*) FROM groups WHERE workspace_id IS NULL")).scalar_one(),
        conn.execute(text("SELECT COUNT(*) FROM hosts")).scalar_one(),
        conn.execute(text("SELECT COUNT(*) FROM hosts WHERE workspace_id IS NOT NULL")).scalar_one(),
        conn.execute(text("SELECT COUNT(*) FROM hosts WHERE workspace_id IS NULL")).scalar_one(),
        conn.execute(text("SELECT COUNT(*) FROM oc_panels")).scalar_one(),
        conn.execute(text("SELECT COUNT(*) FROM oc_panels WHERE workspace_id IS NOT NULL")).scalar_one(),
        conn.execute(text("SELECT COUNT(*) FROM oc_panels WHERE workspace_id IS NULL")).scalar_one(),
    )


def downgrade() -> None:
    pass
