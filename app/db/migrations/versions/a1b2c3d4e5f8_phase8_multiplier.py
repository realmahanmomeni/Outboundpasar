"""phase8_multiplier

Revision ID: a1b2c3d4e5f8
Revises: a1b2c3d4e5f7
Create Date: 2026-09-26 11:15:00.000000

"""
from alembic import op
import sqlalchemy as sa
from app.db.compiles_types import SqliteCompatibleBigInteger

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f8'
down_revision = 'a1b2c3d4e5f7'
branch_labels = None
depends_on = None

def upgrade() -> None:
    with op.batch_alter_table('oc_panels') as batch_op:
        batch_op.alter_column('default_multiplier', new_column_name='multiplier', type_=sa.Numeric(precision=10, scale=2))
        
    with op.batch_alter_table('hosts') as batch_op:
        batch_op.drop_column('multiplier_override')

def downgrade() -> None:
    with op.batch_alter_table('hosts') as batch_op:
        batch_op.add_column(sa.Column('multiplier_override', sa.Numeric(precision=6, scale=4), nullable=True))
        
    with op.batch_alter_table('oc_panels') as batch_op:
        batch_op.alter_column('multiplier', new_column_name='default_multiplier', type_=sa.Numeric(precision=6, scale=4))
