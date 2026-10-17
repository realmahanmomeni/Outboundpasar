"""Per-workspace subscription custom domains (Phase 3-B).

Revision ID: b3c4d5e6f7a8
Revises: f2a3b4c5d6e7
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import app.db.compiles_types

revision = "b3c4d5e6f7a8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "subscription_custom_domains",
        sa.Column("id", app.db.compiles_types.SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("tenant_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("hostname_normalized", sa.String(length=253), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="pending_verification", nullable=False),
        sa.Column("verification_token_hash", sa.String(length=64), nullable=True),
        sa.Column("verification_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("certificate_ref", sa.String(length=64), nullable=True),
        sa.Column("certificate_not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("certificate_last_error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_subscription_custom_domains_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_subscription_custom_domains_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hostname_normalized", name="uq_subscription_custom_domains_hostname"),
    )
    op.create_index(
        "ix_subscription_custom_domains_workspace_id",
        "subscription_custom_domains",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_custom_domains_workspace_id", table_name="subscription_custom_domains")
    op.drop_table("subscription_custom_domains")
