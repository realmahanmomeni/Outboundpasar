"""Phase 3-C: subscription experience templates (additive).

Revision ID: h9c0d1e2f3a4
Revises: g8b9c0d1e2f3
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import app.db.compiles_types

revision = "h9c0d1e2f3a4"
down_revision = "g8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_experience_templates",
        sa.Column("id", app.db.compiles_types.SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("tenant_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True),
        sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.String(length=2048), nullable=True),
        sa.Column("theme", sa.JSON(), nullable=True),
        sa.Column("support_links", sa.JSON(), nullable=True),
        sa.Column("footer_text", sa.Text(), nullable=True),
        sa.Column("custom_blocks", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_subscription_experience_templates_tenant_id_tenants")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_subscription_experience_templates_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_experience_templates")),
        sa.UniqueConstraint(
            "workspace_id",
            "name",
            name=op.f("uq_subscription_experience_templates_workspace_name"),
        ),
    )
    op.create_index(
        "ix_subscription_experience_templates_workspace_id",
        "subscription_experience_templates",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_experience_templates_workspace_id", table_name="subscription_experience_templates")
    op.drop_table("subscription_experience_templates")
