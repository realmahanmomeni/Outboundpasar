"""Add explicit seller group selection to user_panel_bindings (additive, nullable).

Revision ID: k2f3a4b5c6d7
Revises: j1e2f3a4b5c6
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "k2f3a4b5c6d7"
down_revision = "j1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_panel_bindings",
        sa.Column(
            "desired_source_group_ids",
            sa.JSON().with_variant(postgresql.JSONB(none_as_null=True), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("user_panel_bindings", "desired_source_group_ids")
