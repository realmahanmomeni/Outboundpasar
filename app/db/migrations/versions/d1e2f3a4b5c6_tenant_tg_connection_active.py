"""Add active + connected_at on tenant_telegram_connections.

Revision ID: d1e2f3a4b5c6
Revises: c4d5e6f7a8b9
"""
from alembic import op
import sqlalchemy as sa

revision = "d1e2f3a4b5c6"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_telegram_connections",
        sa.Column("active", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "tenant_telegram_connections",
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE tenant_telegram_connections
        SET active = (status = 'active' AND revoked_at IS NULL),
            connected_at = verified_at
        """
    )


def downgrade() -> None:
    op.drop_column("tenant_telegram_connections", "connected_at")
    op.drop_column("tenant_telegram_connections", "active")
