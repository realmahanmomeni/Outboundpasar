"""Bot-provisioned managed subdomain claims (Phase 3-B.3 UX).

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1

FLAG: PHASE_3B3_MANAGED_SUBDOMAIN_BOT_BINDING — additive only.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import app.db.compiles_types

revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_subdomain_bot_bindings",
        sa.Column("id", app.db.compiles_types.SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("binding_admin_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("label", sa.String(length=63), nullable=False),
        sa.Column("fqdn", sa.String(length=253), nullable=False),
        sa.Column("oc_subdomain_id", sa.String(length=128), nullable=False),
        sa.Column("oc_account_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending_claim", nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["binding_admin_id"], ["admins.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("label", name="uq_managed_subdomain_bot_bindings_label"),
        sa.UniqueConstraint("idempotency_key", name="uq_managed_subdomain_bot_bindings_idempotency_key"),
    )
    op.create_index(
        "ix_managed_subdomain_bot_bindings_workspace_id",
        "managed_subdomain_bot_bindings",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_managed_subdomain_bot_bindings_workspace_id", table_name="managed_subdomain_bot_bindings")
    op.drop_table("managed_subdomain_bot_bindings")
