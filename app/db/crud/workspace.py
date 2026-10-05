"""Workspace provisioning for tenant administrators and operators."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, Workspace
from app.models.admin import AdminDetails
from app.services.tenant_admin_scope import (
    BUILTIN_ADMINISTRATOR_ROLE_ID,
    BUILTIN_OPERATOR_ROLE_ID,
    is_builtin_administrator_role,
    is_builtin_operator_role,
)


async def create_workspace_for_administrator(db: AsyncSession, db_admin: Admin) -> Workspace:
    """Create exactly one workspace owned by a tenant administrator."""
    if db_admin.tenant_id is None:
        raise ValueError("Administrator must have tenant_id before workspace creation")
    if db_admin.id is None:
        raise ValueError("Administrator must be persisted before workspace creation")
    existing = await db.scalar(select(Workspace).where(Workspace.owner_admin_id == db_admin.id))
    if existing is not None:
        if db_admin.workspace_id != existing.id:
            db_admin.workspace_id = existing.id
            await db.flush()
        return existing

    workspace = Workspace(tenant_id=db_admin.tenant_id, owner_admin_id=db_admin.id)
    db.add(workspace)
    await db.flush()
    db_admin.workspace_id = workspace.id
    await db.flush()
    return workspace


async def sole_workspace_id_in_tenant(db: AsyncSession, tenant_id: int) -> int | None:
    """Return the workspace id when the tenant has exactly one workspace."""
    rows = (
        await db.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
    ).scalars().all()
    if len(rows) != 1:
        return None
    return int(rows[0])


async def assign_workspace_for_new_admin(
    db: AsyncSession,
    db_admin: Admin,
    *,
    creator: AdminDetails,
) -> None:
    """Set workspace membership for a newly created admin (no client-supplied workspace)."""
    if db_admin.role_id == 1:
        return

    role = db_admin.role
    if role is not None:
        if role.is_owner:
            return
        if is_builtin_administrator_role(role):
            await create_workspace_for_administrator(db, db_admin)
            return
        if is_builtin_operator_role(role):
            workspace_id = await _operator_workspace_id(db, creator)
            if workspace_id is not None:
                db_admin.workspace_id = workspace_id
                await db.flush()
            return

    if db_admin.role_id == BUILTIN_ADMINISTRATOR_ROLE_ID:
        await create_workspace_for_administrator(db, db_admin)
        return
    if db_admin.role_id == BUILTIN_OPERATOR_ROLE_ID:
        workspace_id = await _operator_workspace_id(db, creator)
        if workspace_id is not None:
            db_admin.workspace_id = workspace_id
            await db.flush()


async def _operator_workspace_id(db: AsyncSession, creator: AdminDetails) -> int | None:
    if creator.is_owner:
        if creator.tenant_id is not None:
            return await sole_workspace_id_in_tenant(db, creator.tenant_id)
        return None
    if creator.workspace_id is not None:
        return creator.workspace_id
    if creator.id is None:
        return None
    db_creator = await db.get(Admin, creator.id)
    if db_creator is not None and db_creator.workspace_id is not None:
        return db_creator.workspace_id
    if creator.tenant_id is not None:
        return await sole_workspace_id_in_tenant(db, creator.tenant_id)
    return None


async def ensure_administrator_has_workspace(db: AsyncSession, db_admin: Admin) -> None:
    """Idempotently attach a workspace when an admin becomes a tenant administrator."""
    if db_admin.role_id == BUILTIN_ADMINISTRATOR_ROLE_ID or (
        db_admin.role is not None and is_builtin_administrator_role(db_admin.role)
    ):
        await create_workspace_for_administrator(db, db_admin)
