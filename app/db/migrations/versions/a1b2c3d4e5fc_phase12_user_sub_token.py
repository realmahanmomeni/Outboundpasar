"""phase12_user_sub_token

Revision ID: a1b2c3d4e5fc
Revises: a1b2c3d4e5fb
Create Date: 2026-09-27 12:00:00.000000

"""
import secrets
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5fc'
down_revision = 'a1b2c3d4e5fb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Add column sub_token as nullable
    op.add_column('users', sa.Column('sub_token', sa.String(length=64), nullable=True))

    # Step 2: Backfill existing users with unique cryptographically random tokens
    bind = op.get_bind()
    users_table = sa.table(
        'users',
        sa.column('id', sa.BigInteger),
        sa.column('sub_token', sa.String),
    )
    results = bind.execute(sa.select(users_table.c.id)).fetchall()
    tokens = set()
    for row in results:
        user_id = row[0]
        while True:
            t = secrets.token_hex(16)
            if t not in tokens:
                tokens.add(t)
                break
        bind.execute(
            users_table.update().where(users_table.c.id == user_id).values(sub_token=t)
        )

    # Step 3: Verify uniqueness
    distinct_count = bind.execute(sa.text("SELECT count(DISTINCT sub_token) FROM users")).scalar()
    total_count = bind.execute(sa.text("SELECT count(*) FROM users")).scalar()
    if distinct_count != total_count:
        raise ValueError(f"Uniqueness verification failed: {distinct_count} distinct tokens for {total_count} users")

    # Step 4: Add unique index constraint
    op.create_index(op.f('ix_users_sub_token'), 'users', ['sub_token'], unique=True)

    # Step 5: Enforce non-null
    op.alter_column('users', 'sub_token', nullable=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_users_sub_token'), table_name='users')
    op.drop_column('users', 'sub_token')
