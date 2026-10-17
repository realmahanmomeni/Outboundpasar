"""Managed subdomain hardening: connection token lookup + API nonces.

Revision ID: g8b9c0d1e2f3
Revises: f7a8b9c0d1e2

FLAG: PHASE_3B3_MANAGED_SUBDOMAIN_HARDENING — additive only.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "g8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_telegram_connections",
        sa.Column("oc_connection_token_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_tenant_telegram_connections_token_sha256",
        "tenant_telegram_connections",
        ["oc_connection_token_sha256"],
        unique=False,
    )
    op.create_table(
        "integration_api_nonces",
        sa.Column("nonce_sha256", sa.String(length=64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_integration_api_nonces_expires_at", "integration_api_nonces", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_integration_api_nonces_expires_at", table_name="integration_api_nonces")
    op.drop_table("integration_api_nonces")
    op.drop_index("ix_tenant_telegram_connections_token_sha256", table_name="tenant_telegram_connections")
    op.drop_column("tenant_telegram_connections", "oc_connection_token_sha256")
