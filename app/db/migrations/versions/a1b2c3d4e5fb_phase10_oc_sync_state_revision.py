"""phase10_oc_sync_state_revision

Revision ID: a1b2c3d4e5fb
Revises: a1b2c3d4e5fa
Create Date: 2026-09-26 16:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5fb'
down_revision = 'a1b2c3d4e5fa'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Add revision column with a default of 1
    with op.batch_alter_table('oc_sync_states') as batch_op:
        batch_op.add_column(sa.Column('revision', sa.Integer, server_default='1', nullable=False))

def downgrade() -> None:
    with op.batch_alter_table('oc_sync_states') as batch_op:
        batch_op.drop_column('revision')
