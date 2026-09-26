"""phase10_last_synced_configs

Revision ID: a1b2c3d4e5fa
Revises: a1b2c3d4e5f9
Create Date: 2026-09-26 15:38:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5fa'
down_revision = 'a1b2c3d4e5f9'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Add last_synced_configs to oc_user_mappings
    with op.batch_alter_table('oc_user_mappings') as batch_op:
        batch_op.add_column(sa.Column('last_synced_configs', sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('oc_user_mappings') as batch_op:
        batch_op.drop_column('last_synced_configs')
