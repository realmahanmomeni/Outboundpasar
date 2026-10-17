"""Settings, user_templates, and client_templates workspace_id (Phase 5).

Revision ID: e5f6a7b8c9d5
Revises: d4e5f6a7b8c4
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

import app.db.compiles_types

revision = "e5f6a7b8c9d5"
down_revision = "d4e5f6a7b8c4"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def _drop_unique_if_exists(table: str, *constraint_names: str) -> None:
    conn = op.get_bind()
    for name in constraint_names:
        conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"'))


def upgrade() -> None:
    _add_settings_workspace_columns()
    _add_user_template_workspace_columns()
    _add_client_template_workspace_columns()

    conn = op.get_bind()
    _backfill_user_template_workspaces(conn)
    _log_migration_stats(conn)


def _add_settings_workspace_columns() -> None:
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("tenant_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True)
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_settings_tenant_id_tenants"),
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_settings_workspace_id_workspaces"),
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(op.f("ix_settings_workspace_id"), ["workspace_id"], unique=True)


def _add_user_template_workspace_columns() -> None:
    _drop_unique_if_exists("user_templates", "user_templates_name_key")
    with op.batch_alter_table("user_templates", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("tenant_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True)
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_user_templates_tenant_id_tenants"),
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_user_templates_workspace_id_workspaces"),
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(op.f("ix_user_templates_workspace_id"), ["workspace_id"], unique=False)
        batch_op.create_unique_constraint(
            "uq_user_templates_workspace_id_name",
            ["workspace_id", "name"],
        )


def _add_client_template_workspace_columns() -> None:
    _drop_unique_if_exists(
        "client_templates",
        "client_templates_template_type_name_key",
        "uq_client_templates_template_type_name",
    )
    with op.batch_alter_table("client_templates", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("tenant_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True)
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_client_templates_tenant_id_tenants"),
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_client_templates_workspace_id_workspaces"),
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(op.f("ix_client_templates_workspace_id"), ["workspace_id"], unique=False)
        batch_op.create_unique_constraint(
            "uq_client_templates_workspace_type_name",
            ["workspace_id", "template_type", "name"],
        )


def _backfill_user_template_workspaces(conn) -> None:
    conn.execute(
        text(
            """
            UPDATE user_templates AS ut
            SET workspace_id = sub.ws,
                tenant_id = sub.tid
            FROM (
                SELECT tga.user_template_id AS template_id,
                       MIN(g.workspace_id) AS ws,
                       MIN(g.tenant_id) AS tid
                FROM template_group_association AS tga
                JOIN groups AS g ON g.id = tga.group_id
                WHERE g.workspace_id IS NOT NULL
                GROUP BY tga.user_template_id
                HAVING COUNT(DISTINCT g.workspace_id) = 1
                   AND COUNT(DISTINCT g.tenant_id) <= 1
            ) AS sub
            WHERE ut.id = sub.template_id
              AND ut.workspace_id IS NULL
            """
        )
    )


def _log_migration_stats(conn) -> None:
    settings_global = conn.execute(
        text("SELECT COUNT(*) FROM settings WHERE workspace_id IS NULL")
    ).scalar_one()
    settings_ws = conn.execute(
        text("SELECT COUNT(*) FROM settings WHERE workspace_id IS NOT NULL")
    ).scalar_one()
    ut_assigned = conn.execute(
        text("SELECT COUNT(*) FROM user_templates WHERE workspace_id IS NOT NULL")
    ).scalar_one()
    ut_unassigned = conn.execute(
        text("SELECT COUNT(*) FROM user_templates WHERE workspace_id IS NULL")
    ).scalar_one()
    ct_system = conn.execute(
        text("SELECT COUNT(*) FROM client_templates WHERE is_system IS TRUE AND workspace_id IS NULL")
    ).scalar_one()
    ct_custom_assigned = conn.execute(
        text(
            "SELECT COUNT(*) FROM client_templates "
            "WHERE is_system IS NOT TRUE AND workspace_id IS NOT NULL"
        )
    ).scalar_one()
    ct_custom_unassigned = conn.execute(
        text(
            "SELECT COUNT(*) FROM client_templates "
            "WHERE is_system IS NOT TRUE AND workspace_id IS NULL"
        )
    ).scalar_one()
    logger.info(
        "Phase 5 migration stats: settings global=%s workspace=%s; "
        "user_templates assigned=%s unassigned=%s; "
        "client_templates system=%s custom_assigned=%s custom_unassigned=%s",
        settings_global,
        settings_ws,
        ut_assigned,
        ut_unassigned,
        ct_system,
        ct_custom_assigned,
        ct_custom_unassigned,
    )


def downgrade() -> None:
    with op.batch_alter_table("client_templates", schema=None) as batch_op:
        batch_op.drop_constraint("uq_client_templates_workspace_type_name", type_="unique")
        batch_op.create_unique_constraint("client_templates_template_type_name_key", ["template_type", "name"])
        batch_op.drop_index(op.f("ix_client_templates_workspace_id"))
        batch_op.drop_constraint(batch_op.f("fk_client_templates_workspace_id_workspaces"), type_="foreignkey")
        batch_op.drop_constraint(batch_op.f("fk_client_templates_tenant_id_tenants"), type_="foreignkey")
        batch_op.drop_column("workspace_id")
        batch_op.drop_column("tenant_id")

    with op.batch_alter_table("user_templates", schema=None) as batch_op:
        batch_op.drop_constraint("uq_user_templates_workspace_id_name", type_="unique")
        batch_op.create_unique_constraint("user_templates_name_key", ["name"])
        batch_op.drop_index(op.f("ix_user_templates_workspace_id"))
        batch_op.drop_constraint(batch_op.f("fk_user_templates_workspace_id_workspaces"), type_="foreignkey")
        batch_op.drop_constraint(batch_op.f("fk_user_templates_tenant_id_tenants"), type_="foreignkey")
        batch_op.drop_column("workspace_id")
        batch_op.drop_column("tenant_id")

    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_settings_workspace_id"))
        batch_op.drop_constraint(batch_op.f("fk_settings_workspace_id_workspaces"), type_="foreignkey")
        batch_op.drop_constraint(batch_op.f("fk_settings_tenant_id_tenants"), type_="foreignkey")
        batch_op.drop_column("workspace_id")
        batch_op.drop_column("tenant_id")
