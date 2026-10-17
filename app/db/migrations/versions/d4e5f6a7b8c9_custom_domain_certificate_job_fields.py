"""Custom domain certificate job bookkeeping (Phase 3-B.2).

Revision ID: d4e5f6a7b8c9
Revises: b3c4d5e6f7a8
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_custom_domains",
        sa.Column("certificate_issue_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "subscription_custom_domains",
        sa.Column("certificate_issue_locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column(
        "subscription_custom_domains",
        "certificate_ref",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "subscription_custom_domains",
        "certificate_ref",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.drop_column("subscription_custom_domains", "certificate_issue_locked_until")
    op.drop_column("subscription_custom_domains", "certificate_issue_attempts")
