"""Phase 3-D: workspace subscription branding profiles (additive).

Revision ID: i0d1e2f3a4b5
Revises: h9c0d1e2f3a4
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import app.db.compiles_types

revision = "i0d1e2f3a4b5"
down_revision = "h9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_branding_profiles",
        sa.Column("id", app.db.compiles_types.SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("tenant_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True),
        sa.Column("created_by_admin_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True),
        sa.Column("brand_name", sa.String(length=256), nullable=True),
        sa.Column("logo_url", sa.String(length=2048), nullable=True),
        sa.Column("support_url", sa.String(length=2048), nullable=True),
        sa.Column("telegram_url", sa.String(length=2048), nullable=True),
        sa.Column("welcome_message", sa.Text(), nullable=True),
        sa.Column("footer_text", sa.Text(), nullable=True),
        sa.Column("primary_color", sa.String(length=64), nullable=True),
        sa.Column("secondary_color", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_subscription_branding_profiles_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_subscription_branding_profiles_tenant_id_tenants"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_admin_id"],
            ["admins.id"],
            name=op.f("fk_subscription_branding_profiles_created_by_admin_id_admins"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_branding_profiles")),
        sa.UniqueConstraint("workspace_id", name=op.f("uq_subscription_branding_profiles_workspace_id")),
    )


def downgrade() -> None:
    op.drop_table("subscription_branding_profiles")
