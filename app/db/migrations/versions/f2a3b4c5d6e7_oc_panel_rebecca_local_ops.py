"""OCPanel panel_type and encrypted buyer sub-admin credentials for Rebecca local ops.

Revision ID: f2a3b4c5d6e7
Revises: e5f6a7b8c9d5
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
down_revision = "e5f6a7b8c9d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("oc_panels", sa.Column("panel_type", sa.String(length=32), nullable=True))
    op.add_column("oc_panels", sa.Column("buyer_panel_url", sa.String(length=1024), nullable=True))
    op.add_column("oc_panels", sa.Column("buyer_sub_admin_username", sa.String(length=256), nullable=True))
    op.add_column(
        "oc_panels",
        sa.Column("buyer_sub_admin_password_encrypted", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("oc_panels", "buyer_sub_admin_password_encrypted")
    op.drop_column("oc_panels", "buyer_sub_admin_username")
    op.drop_column("oc_panels", "buyer_panel_url")
    op.drop_column("oc_panels", "panel_type")
