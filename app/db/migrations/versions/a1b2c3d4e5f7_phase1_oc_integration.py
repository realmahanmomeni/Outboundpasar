"""phase1_oc_integration

Revision ID: a1b2c3d4e5f7
Revises: 48a6bcb8bba1
Create Date: 2026-09-21 14:35:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from app.db.compiles_types import SqliteCompatibleBigInteger

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f7'
down_revision = '48a6bcb8bba1'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # OCIntegration
    op.create_table('oc_integrations',
    sa.Column('id', SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
    sa.Column('base_url', sa.String(length=1024), nullable=False),
    sa.Column('api_token_encrypted', sa.String(length=2048), nullable=False),
    sa.Column('token_preview', sa.String(length=64), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oc_integrations'))
    )

    # OCPanel
    op.create_table('oc_panels',
    sa.Column('id', SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
    sa.Column('integration_id', SqliteCompatibleBigInteger(), nullable=False),
    sa.Column('source_panel_id', sa.String(length=256), nullable=False),
    sa.Column('purchaser_identity', sa.String(length=256), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('default_multiplier', sa.Numeric(precision=6, scale=4), nullable=False),
    sa.Column('test_user_id', sa.String(length=256), nullable=True),
    sa.Column('sync_status', sa.String(length=64), nullable=True),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['integration_id'], ['oc_integrations.id'], name=op.f('fk_oc_panels_integration_id_oc_integrations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oc_panels'))
    )

    # OCPanelGroup
    op.create_table('oc_panel_groups',
    sa.Column('id', SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
    sa.Column('panel_id', SqliteCompatibleBigInteger(), nullable=False),
    sa.Column('source_group_id', sa.String(length=256), nullable=False),
    sa.Column('source_name', sa.String(length=256), nullable=False),
    sa.Column('is_selected', sa.Boolean(), nullable=False),
    sa.Column('local_group_id', SqliteCompatibleBigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['local_group_id'], ['groups.id'], name=op.f('fk_oc_panel_groups_local_group_id_groups'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['panel_id'], ['oc_panels.id'], name=op.f('fk_oc_panel_groups_panel_id_oc_panels'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oc_panel_groups')),
    sa.UniqueConstraint('panel_id', 'source_group_id', name=op.f('uq_oc_panel_groups_panel_id'))
    )

    # OCPanelConfig
    op.create_table('oc_panel_configs',
    sa.Column('id', SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
    sa.Column('panel_id', SqliteCompatibleBigInteger(), nullable=False),
    sa.Column('panel_group_id', SqliteCompatibleBigInteger(), nullable=True),
    sa.Column('source_config_id', sa.String(length=256), nullable=False),
    sa.Column('source_name', sa.String(length=256), nullable=False),
    sa.Column('protocol', sa.String(length=64), nullable=True),
    sa.Column('network', sa.String(length=64), nullable=True),
    sa.Column('port', sa.Integer(), nullable=True),
    sa.Column('source_payload', sa.JSON().with_variant(postgresql.JSONB(none_as_null=True), 'postgresql'), nullable=True),
    sa.Column('virtual_inbound_tag', sa.String(length=256), nullable=True),
    sa.Column('source_missing', sa.Boolean(), nullable=False),
    sa.Column('locally_hidden', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['panel_group_id'], ['oc_panel_groups.id'], name=op.f('fk_oc_panel_configs_panel_group_id_oc_panel_groups'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['panel_id'], ['oc_panels.id'], name=op.f('fk_oc_panel_configs_panel_id_oc_panels'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['virtual_inbound_tag'], ['inbounds.tag'], name=op.f('fk_oc_panel_configs_virtual_inbound_tag_inbounds'), onupdate='CASCADE', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oc_panel_configs')),
    sa.UniqueConstraint('panel_id', 'source_config_id', name=op.f('uq_oc_panel_configs_panel_id'))
    )

    # OCUserMapping
    op.create_table('oc_user_mappings',
    sa.Column('id', SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
    sa.Column('user_id', SqliteCompatibleBigInteger(), nullable=False),
    sa.Column('panel_id', SqliteCompatibleBigInteger(), nullable=False),
    sa.Column('external_user_id', sa.String(length=256), nullable=False),
    sa.Column('last_cumulative_traffic', SqliteCompatibleBigInteger(), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['panel_id'], ['oc_panels.id'], name=op.f('fk_oc_user_mappings_panel_id_oc_panels'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_oc_user_mappings_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oc_user_mappings')),
    sa.UniqueConstraint('user_id', 'panel_id', name=op.f('uq_oc_user_mappings_user_id'))
    )

    # OCSyncState
    op.create_table('oc_sync_states',
    sa.Column('id', SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
    sa.Column('entity_type', sa.String(length=64), nullable=False),
    sa.Column('entity_id', sa.String(length=256), nullable=False),
    sa.Column('operation', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(none_as_null=True), 'postgresql'), nullable=True),
    sa.Column('status', sa.String(length=64), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.String(length=2048), nullable=True),
    sa.Column('idempotency_key', sa.String(length=256), nullable=False),
    sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oc_sync_states')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_oc_sync_states_idempotency_key'))
    )

    # Add multiplier_override to hosts
    op.add_column('hosts', sa.Column('multiplier_override', sa.Numeric(precision=6, scale=4), nullable=True))


def downgrade() -> None:
    op.drop_column('hosts', 'multiplier_override')
    op.drop_table('oc_sync_states')
    op.drop_table('oc_user_mappings')
    op.drop_table('oc_panel_configs')
    op.drop_table('oc_panel_groups')
    op.drop_table('oc_panels')
    op.drop_table('oc_integrations')
