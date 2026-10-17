"""Administrator workspaces and OCPanel.workspace_id (Phase 1).

Revision ID: e8f9a0b1c2d3
Revises: c6d7e8f9a0b1
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

import app.db.compiles_types

revision = "e8f9a0b1c2d3"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

BUILTIN_ADMINISTRATOR_ROLE_ID = 2
BUILTIN_OPERATOR_ROLE_ID = 3
OWNER_ROLE_ID = 1


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", app.db.compiles_types.SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("owner_admin_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name=op.f("fk_workspaces_tenant_id_tenants"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["owner_admin_id"],
            ["admins.id"],
            name=op.f("fk_workspaces_owner_admin_id_admins"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("owner_admin_id", name=op.f("uq_workspaces_owner_admin_id")),
    )
    op.create_index(op.f("ix_workspaces_tenant_id"), "workspaces", ["tenant_id"], unique=False)

    with op.batch_alter_table("admins", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True)
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_admins_workspace_id_workspaces"),
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("oc_panels", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("workspace_id", app.db.compiles_types.SqliteCompatibleBigInteger(), nullable=True)
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_oc_panels_workspace_id_workspaces"),
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(op.f("ix_oc_panels_workspace_id"), ["workspace_id"], unique=False)

    conn = op.get_bind()
    _backfill_workspaces_and_panels(conn)


def _backfill_workspaces_and_panels(conn) -> None:
    admin_count = conn.execute(text("SELECT COUNT(*) FROM admins")).scalar_one()
    operator_count = conn.execute(
        text("SELECT COUNT(*) FROM admins WHERE role_id = :rid"),
        {"rid": BUILTIN_OPERATOR_ROLE_ID},
    ).scalar_one()
    panel_count = conn.execute(text("SELECT COUNT(*) FROM oc_panels")).scalar_one()
    tg_count = conn.execute(text("SELECT COUNT(*) FROM tenant_telegram_connections")).scalar_one()

    admins = conn.execute(
        text(
            "SELECT id, tenant_id, role_id FROM admins "
            "WHERE role_id = :admin_role AND tenant_id IS NOT NULL"
        ),
        {"admin_role": BUILTIN_ADMINISTRATOR_ROLE_ID},
    ).fetchall()

    workspaces_created = 0
    for admin_id, tenant_id, _role_id in admins:
        existing_ws = conn.execute(
            text("SELECT id FROM workspaces WHERE owner_admin_id = :aid"),
            {"aid": admin_id},
        ).scalar_one_or_none()
        if existing_ws is not None:
            conn.execute(
                text("UPDATE admins SET workspace_id = :wid WHERE id = :aid AND workspace_id IS NULL"),
                {"wid": existing_ws, "aid": admin_id},
            )
            continue
        ws_id = conn.execute(
            text(
                "INSERT INTO workspaces (tenant_id, owner_admin_id, created_at, updated_at) "
                "VALUES (:tid, :aid, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING id"
            ),
            {"tid": tenant_id, "aid": admin_id},
        ).scalar_one()
        conn.execute(
            text("UPDATE admins SET workspace_id = :wid WHERE id = :aid"),
            {"wid": ws_id, "aid": admin_id},
        )
        workspaces_created += 1

    operators_assigned = 0
    operators_ambiguous = 0
    tenant_ids = conn.execute(
        text("SELECT DISTINCT tenant_id FROM admins WHERE role_id = :rid AND tenant_id IS NOT NULL"),
        {"rid": BUILTIN_OPERATOR_ROLE_ID},
    ).fetchall()
    for (tenant_id,) in tenant_ids:
        ws_rows = conn.execute(
            text("SELECT id FROM workspaces WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        ).fetchall()
        if len(ws_rows) != 1:
            operators_ambiguous += conn.execute(
                text(
                    "SELECT COUNT(*) FROM admins WHERE tenant_id = :tid AND role_id = :rid AND workspace_id IS NULL"
                ),
                {"tid": tenant_id, "rid": BUILTIN_OPERATOR_ROLE_ID},
            ).scalar_one()
            continue
        sole_ws = ws_rows[0][0]
        result = conn.execute(
            text(
                "UPDATE admins SET workspace_id = :wid "
                "WHERE tenant_id = :tid AND role_id = :rid AND workspace_id IS NULL"
            ),
            {"wid": sole_ws, "tid": tenant_id, "rid": BUILTIN_OPERATOR_ROLE_ID},
        )
        operators_assigned += result.rowcount or 0

    panels_assigned = 0
    panels_ambiguous = 0
    panels = conn.execute(
        text("SELECT id, tenant_id, oc_account_id FROM oc_panels WHERE workspace_id IS NULL")
    ).fetchall()
    for panel_id, tenant_id, oc_account_id in panels:
        if tenant_id is None or oc_account_id is None:
            panels_ambiguous += 1
            continue
        bindings = conn.execute(
            text(
                "SELECT DISTINCT binding_admin_id FROM tenant_telegram_connections "
                "WHERE tenant_id = :tid AND oc_account_id = :oc AND binding_admin_id IS NOT NULL"
            ),
            {"tid": tenant_id, "oc": oc_account_id},
        ).fetchall()
        admin_ids = [row[0] for row in bindings if row[0] is not None]
        if len(admin_ids) != 1:
            panels_ambiguous += 1
            continue
        ws_id = conn.execute(
            text("SELECT workspace_id FROM admins WHERE id = :aid"),
            {"aid": admin_ids[0]},
        ).scalar_one_or_none()
        if ws_id is None:
            panels_ambiguous += 1
            continue
        conn.execute(
            text("UPDATE oc_panels SET workspace_id = :wid WHERE id = :pid"),
            {"wid": ws_id, "pid": panel_id},
        )
        panels_assigned += 1

    workspace_count = conn.execute(text("SELECT COUNT(*) FROM workspaces")).scalar_one()
    unassigned_panels = conn.execute(
        text("SELECT COUNT(*) FROM oc_panels WHERE workspace_id IS NULL")
    ).scalar_one()

    logger.info(
        "Phase1 workspace migration: admins=%s operators=%s tg_connections=%s oc_panels=%s "
        "workspaces_created=%s workspaces_total=%s operators_assigned=%s operators_ambiguous_tenants=%s "
        "panels_assigned=%s panels_ambiguous=%s panels_unassigned=%s",
        admin_count,
        operator_count,
        tg_count,
        panel_count,
        workspaces_created,
        workspace_count,
        operators_assigned,
        operators_ambiguous,
        panels_assigned,
        panels_ambiguous,
        unassigned_panels,
    )


def downgrade() -> None:
    with op.batch_alter_table("oc_panels", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_oc_panels_workspace_id"))
        batch_op.drop_constraint(batch_op.f("fk_oc_panels_workspace_id_workspaces"), type_="foreignkey")
        batch_op.drop_column("workspace_id")

    with op.batch_alter_table("admins", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_admins_workspace_id_workspaces"), type_="foreignkey")
        batch_op.drop_column("workspace_id")

    op.drop_index(op.f("ix_workspaces_tenant_id"), table_name="workspaces")
    op.drop_table("workspaces")
