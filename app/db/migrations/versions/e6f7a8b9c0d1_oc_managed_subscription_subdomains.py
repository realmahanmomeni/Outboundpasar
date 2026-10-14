"""OC managed subscription subdomains (Phase 3-B.3 re-scope).

Revision ID: e6f7a8b9c0d1
Revises: d4e5f6a7b8c9

FLAG: PHASE_3B3_MANAGED_SUBDOMAIN — additive columns only; safe to skip on rollback.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6f7a8b9c0d1"
down_revision = "d4e5f6a7b8c9"
branch_labels = ("phase_3b3_managed_subdomain",)
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_custom_domains",
        sa.Column("domain_kind", sa.String(length=32), server_default="arbitrary", nullable=False),
    )
    op.add_column(
        "subscription_custom_domains",
        sa.Column("oc_subdomain_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "subscription_custom_domains",
        sa.Column("oc_subdomain_label", sa.String(length=63), nullable=True),
    )
    op.add_column(
        "subscription_custom_domains",
        sa.Column("oc_owner_account_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "subscription_custom_domains",
        sa.Column("oc_last_validated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscription_custom_domains", "oc_last_validated_at")
    op.drop_column("subscription_custom_domains", "oc_owner_account_id")
    op.drop_column("subscription_custom_domains", "oc_subdomain_label")
    op.drop_column("subscription_custom_domains", "oc_subdomain_id")
    op.drop_column("subscription_custom_domains", "domain_kind")
