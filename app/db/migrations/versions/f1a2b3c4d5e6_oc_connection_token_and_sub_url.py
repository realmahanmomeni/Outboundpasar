"""Add OC connection token, pending bot url and mapping subscription url.

Purely additive (nullable columns); no data is modified or removed.

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
"""
from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_telegram_connections",
        sa.Column("oc_connection_token_encrypted", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "tenant_telegram_connections",
        sa.Column("pending_bot_url", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "oc_user_mappings",
        sa.Column("external_subscription_url", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("oc_user_mappings", "external_subscription_url")
    op.drop_column("tenant_telegram_connections", "pending_bot_url")
    op.drop_column("tenant_telegram_connections", "oc_connection_token_encrypted")
