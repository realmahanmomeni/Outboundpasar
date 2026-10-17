"""phase8_multiplier_fix

Revision ID: a1b2c3d4e5f9
Revises: a1b2c3d4e5f8
Create Date: 2026-09-26 11:35:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f9'
down_revision = 'a1b2c3d4e5f8'
branch_labels = None
depends_on = None

def upgrade() -> None:
    with op.batch_alter_table('oc_panels') as batch_op:
        batch_op.alter_column('multiplier', type_=sa.Numeric(precision=32, scale=2), existing_type=sa.Numeric(precision=10, scale=2))

def downgrade() -> None:
    with op.batch_alter_table('oc_panels') as batch_op:
        batch_op.alter_column('multiplier', type_=sa.Numeric(precision=10, scale=2), existing_type=sa.Numeric(precision=32, scale=2))
