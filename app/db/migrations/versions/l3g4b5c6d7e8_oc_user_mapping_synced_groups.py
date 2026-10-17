"""Track the seller groups applied to each OC user mapping (additive, nullable).

Revision ID: l3g4b5c6d7e8
Revises: k2f3a4b5c6d7

The column is NULL for existing rows (applied groups unknown). It is only written after a successful
seller PUT. ``user_panel_bindings.desired_source_group_ids`` (k2f3a4b5c6d7) is intentionally left in place.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "l3g4b5c6d7e8"
down_revision = "k2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oc_user_mappings",
        sa.Column(
            "last_synced_group_ids",
            sa.JSON().with_variant(postgresql.JSONB(none_as_null=True), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("oc_user_mappings", "last_synced_group_ids")
