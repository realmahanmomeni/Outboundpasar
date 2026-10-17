"""tenant telegram connection and oc_account_id on panels

Revision ID: c4d5e6f7a8b9
Revises: db17ab7db94a
Create Date: 2026-10-03

"""
from alembic import op
import sqlalchemy as sa
import app.db.compiles_types

revision = "c4d5e6f7a8b9"
down_revision = "db17ab7db94a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_telegram_connections",
        sa.Column("id", app.db.compiles_types.SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("oc_account_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("pending_intent_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("oc_panels", schema=None) as batch_op:
        batch_op.add_column(sa.Column("oc_account_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("oc_panels", schema=None) as batch_op:
        batch_op.drop_column("oc_account_id")
    op.drop_table("tenant_telegram_connections")
