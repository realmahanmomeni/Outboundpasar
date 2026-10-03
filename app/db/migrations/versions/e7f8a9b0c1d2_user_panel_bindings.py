"""Add user_panel_bindings for explicit OC panel intent.

Revision ID: e7f8a9b0c1d2
Revises: d1e2f3a4b5c6
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "e7f8a9b0c1d2"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_panel_bindings",
        sa.Column("id", SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("user_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("oc_panel_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="explicit"),
        sa.Column(
            "desired_config_ids",
            sa.JSON().with_variant(postgresql.JSONB(none_as_null=True), "postgresql"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_user_panel_bindings_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_panel_bindings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["oc_panel_id"],
            ["oc_panels.id"],
            name=op.f("fk_user_panel_bindings_oc_panel_id_oc_panels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_panel_bindings")),
        sa.UniqueConstraint("user_id", "oc_panel_id", name=op.f("uq_user_panel_bindings_user_id")),
    )


def downgrade() -> None:
    op.drop_table("user_panel_bindings")
