"""add_tenant

Revision ID: a1b2c3d4e5fd
Revises: a1b2c3d4e5fc
Create Date: 2026-10-01 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5fd'
down_revision = 'a1b2c3d4e5fc'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('tenants',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('status', sa.Enum('active', 'disabled', name='tenantstatus', create_constraint=True), server_default='active', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    op.add_column('admins', sa.Column('tenant_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(None, 'admins', 'tenants', ['tenant_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint(None, 'admins', type_='foreignkey')
    op.drop_column('admins', 'tenant_id')
    op.drop_table('tenants')
    tenantstatus = postgresql.ENUM('active', 'disabled', name='tenantstatus')
    tenantstatus.drop(op.get_bind(), checkfirst=True)
