"""Phase 3-E: optional white-label branding fields (additive).

Revision ID: j1e2f3a4b5c6
Revises: i0d1e2f3a4b5
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "j1e2f3a4b5c6"
down_revision = "i0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subscription_branding_profiles", sa.Column("favicon_url", sa.String(length=2048), nullable=True))
    op.add_column("subscription_branding_profiles", sa.Column("custom_css", sa.Text(), nullable=True))
    op.add_column("subscription_branding_profiles", sa.Column("support_text", sa.Text(), nullable=True))
    op.add_column("subscription_branding_profiles", sa.Column("customer_footer", sa.Text(), nullable=True))
    op.add_column(
        "subscription_branding_profiles",
        sa.Column("hide_platform_branding", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("subscription_branding_profiles", "hide_platform_branding")
    op.drop_column("subscription_branding_profiles", "customer_footer")
    op.drop_column("subscription_branding_profiles", "support_text")
    op.drop_column("subscription_branding_profiles", "custom_css")
    op.drop_column("subscription_branding_profiles", "favicon_url")
