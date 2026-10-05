"""Add binding_admin_id to tenant Telegram connections for per-admin OC scope.

Revision ID: c6d7e8f9a0b1
Revises: b4c5d6e7f8a9
"""
from alembic import op
import sqlalchemy as sa

revision = "c6d7e8f9a0b1"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_telegram_connections",
        sa.Column("binding_admin_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_tenant_telegram_connections_binding_admin_id_admins"),
        "tenant_telegram_connections",
        "admins",
        ["binding_admin_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_tenant_telegram_connections_tenant_binding"),
        "tenant_telegram_connections",
        ["tenant_id", "binding_admin_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_telegram_connections_tenant_binding"), table_name="tenant_telegram_connections")
    op.drop_constraint(
        op.f("fk_tenant_telegram_connections_binding_admin_id_admins"),
        "tenant_telegram_connections",
        type_="foreignkey",
    )
    op.drop_column("tenant_telegram_connections", "binding_admin_id")
