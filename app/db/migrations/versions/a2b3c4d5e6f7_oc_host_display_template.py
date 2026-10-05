"""OC panel config display name template (presentation only).

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""
from alembic import op
import sqlalchemy as sa

revision = "a2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oc_panel_configs",
        sa.Column("display_name_template", sa.String(length=1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("oc_panel_configs", "display_name_template")
