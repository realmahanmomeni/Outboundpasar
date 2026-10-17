"""Group and ProxyHost workspace_id for Phase 3 isolation.

Revision ID: c3d4e5f6a7b3
Revises: b2c3d4e5f6a1
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

import app.db.compiles_types

revision = "c3d4e5f6a7b3"
down_revision = "b2c3d4e5f6a1"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    for table in ("groups", "hosts"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True)
            )
            batch_op.create_foreign_key(
                batch_op.f(f"fk_{table}_workspace_id_workspaces"),
                "workspaces",
                ["workspace_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_index(op.f(f"ix_{table}_workspace_id"), ["workspace_id"], unique=False)

    conn = op.get_bind()
    _backfill_group_workspaces(conn)
    _backfill_host_workspaces(conn)
    _log_migration_stats(conn)


def _backfill_group_workspaces(conn) -> None:
    conn.execute(
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


def _backfill_host_workspaces(conn) -> None:
    conn.execute(
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
            """
        )
    )
    conn.execute(
        text(
            """
            UPDATE hosts AS h
            SET workspace_id = sub.wid
            FROM (
                SELECT w.tenant_id, MIN(w.id) AS wid
                FROM workspaces AS w
                GROUP BY w.tenant_id
                HAVING COUNT(*) = 1
            ) AS sub
            WHERE h.tenant_id = sub.tenant_id
              AND h.workspace_id IS NULL
            """
        )
    )


def _log_migration_stats(conn) -> None:
    groups_total = conn.execute(text("SELECT COUNT(*) FROM groups")).scalar_one()
    groups_assigned = conn.execute(
        text("SELECT COUNT(*) FROM groups WHERE workspace_id IS NOT NULL")
    ).scalar_one()
    groups_unassigned = conn.execute(
        text("SELECT COUNT(*) FROM groups WHERE workspace_id IS NULL")
    ).scalar_one()
    groups_ambiguous = conn.execute(
        text(
            """
            SELECT COUNT(*) FROM groups g
            WHERE g.workspace_id IS NULL
              AND EXISTS (
                SELECT 1 FROM users_groups_association uga
                JOIN users u ON u.id = uga.user_id
                WHERE uga.groups_id = g.id AND u.workspace_id IS NOT NULL
              )
            """
        )
    ).scalar_one()

    hosts_total = conn.execute(text("SELECT COUNT(*) FROM hosts")).scalar_one()
    hosts_assigned = conn.execute(
        text("SELECT COUNT(*) FROM hosts WHERE workspace_id IS NOT NULL")
    ).scalar_one()
    hosts_unassigned = conn.execute(
        text("SELECT COUNT(*) FROM hosts WHERE workspace_id IS NULL")
    ).scalar_one()
    hosts_ambiguous = hosts_unassigned

    logger.info(
        "Phase3 group/host workspace migration: groups_total=%s groups_assigned=%s "
        "groups_unassigned=%s groups_ambiguous=%s hosts_total=%s hosts_assigned=%s "
        "hosts_unassigned=%s hosts_ambiguous=%s",
        groups_total,
        groups_assigned,
        groups_unassigned,
        groups_ambiguous,
        hosts_total,
        hosts_assigned,
        hosts_unassigned,
        hosts_ambiguous,
    )


def downgrade() -> None:
    for table in ("hosts", "groups"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(op.f(f"ix_{table}_workspace_id"))
            batch_op.drop_constraint(batch_op.f(f"fk_{table}_workspace_id_workspaces"), type_="foreignkey")
            batch_op.drop_column("workspace_id")
