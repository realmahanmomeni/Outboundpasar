"""Destination subscription hosts for OC panels.

Revision ID: b4c5d6e7f8a9
Revises: a2b3c4d5e6f7
"""
from alembic import op
import sqlalchemy as sa

revision = "b4c5d6e7f8a9"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oc_panel_destination_hosts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("panel_id", sa.BigInteger(), nullable=False),
        sa.Column("destination_config_id", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("virtual_inbound_tag", sa.String(length=256), nullable=True),
        sa.Column("source_payload", sa.JSON(), nullable=True),
        sa.Column("display_name_template", sa.String(length=1024), nullable=True),
        sa.Column("source_missing", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("locally_hidden", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["panel_id"], ["oc_panels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["virtual_inbound_tag"], ["inbounds.tag"], ondelete="SET NULL", onupdate="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("panel_id", "destination_config_id"),
    )


def downgrade() -> None:
    op.drop_table("oc_panel_destination_hosts")
