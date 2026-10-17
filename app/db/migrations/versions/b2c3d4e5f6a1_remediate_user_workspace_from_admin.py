"""Backfill User.workspace_id from Admin.workspace_id where deterministic.

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5fe
"""
from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text

revision = "b2c3d4e5f6a1"
down_revision = "a1b2c3d4e5fe"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()
    before = conn.execute(
        text(
            "SELECT COUNT(*) FROM users u "
            "JOIN admins a ON a.id = u.admin_id "
            "WHERE u.workspace_id IS NULL AND a.workspace_id IS NOT NULL"
        )
    ).scalar_one()
    result = conn.execute(
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
    remediated = result.rowcount or 0
    after = conn.execute(
        text(
            "SELECT COUNT(*) FROM users u "
            "JOIN admins a ON a.id = u.admin_id "
            "WHERE u.workspace_id IS NULL AND a.workspace_id IS NOT NULL"
        )
    ).scalar_one()
    logger.info(
        "Phase2 user workspace remediation: recoverable_before=%s remediated=%s recoverable_after=%s",
        before,
        remediated,
        after,
    )


def downgrade() -> None:
    pass
