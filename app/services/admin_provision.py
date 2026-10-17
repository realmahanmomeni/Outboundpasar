"""Owner-facing admin provisioning options (tenants/workspaces), not raw integration tenants."""
from __future__ import annotations

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, AdminRole, Tenant, TenantStatus, Workspace
from app.db.models_oc import OCPanel
from app.services.tenant_admin_scope import BUILTIN_ADMINISTRATOR_ROLE_ID


async def list_provision_tenants(db: AsyncSession) -> list[tuple[int, str]]:
    """
    Tenants suitable for assigning a new or existing administrator.

    Lists only tenants that already have real product data (administrator workspace
    or imported OC panel). Does not enumerate integration/test tenant rows.
    """
    has_admin_workspace = exists(
        select(Workspace.id)
        .join(Admin, Admin.id == Workspace.owner_admin_id)
        .where(
            Workspace.tenant_id == Tenant.id,
            Admin.role_id == BUILTIN_ADMINISTRATOR_ROLE_ID,
        )
    )
    has_oc_panel = exists(select(OCPanel.id).where(OCPanel.tenant_id == Tenant.id))
    stmt = (
        select(Tenant.id, Tenant.name)
        .where(
            Tenant.status == TenantStatus.active,
            or_(has_admin_workspace, has_oc_panel),
        )
        .order_by(Tenant.name.asc(), Tenant.id.asc())
        .limit(500)
    )
    rows = (await db.execute(stmt)).all()
    return [(int(rid), str(name)) for rid, name in rows]


async def list_provision_workspaces(
    db: AsyncSession,
    *,
    tenant_id: int | None = None,
) -> list[tuple[int, int, str, str]]:
    """
    Workspaces operators may join (tenant_id, workspace_id, owner_username, tenant_name).
    """
    stmt = (
        select(Workspace.id, Workspace.tenant_id, Admin.username, Tenant.name)
        .join(Admin, Admin.id == Workspace.owner_admin_id)
        .join(Tenant, Tenant.id == Workspace.tenant_id)
        .where(Tenant.status == TenantStatus.active)
    )
    if tenant_id is not None:
        stmt = stmt.where(Workspace.tenant_id == tenant_id)
    stmt = stmt.order_by(Tenant.name.asc(), Admin.username.asc()).limit(500)
    rows = (await db.execute(stmt)).all()
    return [(int(wid), int(tid), str(owner), str(tname)) for wid, tid, owner, tname in rows]


async def create_dedicated_tenant_for_administrator(db: AsyncSession, username: str) -> int:
    """Create an isolated org tenant for a new tenant administrator."""
    tenant = Tenant(name=f"Administrator: {username}", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    return int(tenant.id)


async def auto_provision_administrator_scope(db: AsyncSession, db_admin: Admin) -> None:
    """
    Single source of truth: dedicated tenant + owned workspace for a tenant administrator.

    Used when the platform owner creates an administrator (no manual tenant picker).
    """
    from app.db.crud.workspace import create_workspace_for_administrator
    from app.services.tenant_admin_scope import (
        BUILTIN_ADMINISTRATOR_ROLE_ID,
        is_builtin_administrator_role,
    )

    role = db_admin.role
    if role is not None and role.is_owner:
        return
    if db_admin.role_id != BUILTIN_ADMINISTRATOR_ROLE_ID and not is_builtin_administrator_role(role):
        return

    if db_admin.tenant_id is None:
        label = (db_admin.username or "").strip() or f"admin-{db_admin.id}"
        db_admin.tenant_id = await create_dedicated_tenant_for_administrator(db, label)
        await db.flush()

    await create_workspace_for_administrator(db, db_admin)


async def repair_unscoped_administrator(db: AsyncSession, db_admin: Admin) -> bool:
    """Idempotently fix administrators missing tenant/workspace scope. Returns True if mutated."""
    from app.db.crud.workspace import ensure_administrator_has_workspace
    from app.services.tenant_admin_scope import BUILTIN_ADMINISTRATOR_ROLE_ID, is_builtin_administrator_role

    role = db_admin.role
    if role is not None and role.is_owner:
        return False
    if db_admin.role_id != BUILTIN_ADMINISTRATOR_ROLE_ID and not is_builtin_administrator_role(role):
        return False
    before = (db_admin.tenant_id, db_admin.workspace_id)
    await auto_provision_administrator_scope(db, db_admin)
    await ensure_administrator_has_workspace(db, db_admin)
    await db.flush()
    return (db_admin.tenant_id, db_admin.workspace_id) != before
