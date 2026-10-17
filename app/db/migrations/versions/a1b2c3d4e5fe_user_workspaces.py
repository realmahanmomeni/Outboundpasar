"""User.workspace_id for workspace isolation (Phase 2).

Revision ID: a1b2c3d4e5fe
Revises: e8f9a0b1c2d3
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

import app.db.compiles_types

revision = "a1b2c3d4e5fe"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True)
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_users_workspace_id_workspaces"),
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(op.f("ix_users_workspace_id"), ["workspace_id"], unique=False)

    conn = op.get_bind()
    _backfill_user_workspaces(conn)


def _backfill_user_workspaces(conn) -> None:
    users_total = conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one()

    conn.execute(
        text(
            """
            UPDATE users AS u
            SET workspace_id = a.workspace_id
            FROM admins AS a
            WHERE u.admin_id = a.id
              AND u.workspace_id IS NULL
              AND a.workspace_id IS NOT NULL
            """
        )
    )

    users_assigned = conn.execute(
        text("SELECT COUNT(*) FROM users WHERE workspace_id IS NOT NULL")
    ).scalar_one()
    users_unassigned = conn.execute(
        text("SELECT COUNT(*) FROM users WHERE workspace_id IS NULL")
    ).scalar_one()

    ambiguous_rows = conn.execute(
        text(
            """
            SELECT COUNT(*) FROM users u
            WHERE u.admin_id IS NOT NULL
              AND u.workspace_id IS NULL
            """
        )
    ).scalar_one()

    logger.info(
        "Phase2 user workspace migration: users_total=%s users_assigned=%s "
        "users_unassigned=%s users_still_without_workspace_after_admin_join=%s",
        users_total,
        users_assigned,
        users_unassigned,
        ambiguous_rows,
    )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_users_workspace_id"))
        batch_op.drop_constraint(batch_op.f("fk_users_workspace_id_workspaces"), type_="foreignkey")
        batch_op.drop_column("workspace_id")
