"""Workspace resolution and panel access for tenant administrators and operators."""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, User
from app.db.models_oc import OCPanel
from app.models.admin import AdminDetails


async def resolve_admin_workspace_id(
    db: AsyncSession,
    admin: AdminDetails,
    is_owner: bool,
) -> int | None:
    if is_owner or admin.is_owner:
        return None
    if admin.workspace_id is not None:
        return admin.workspace_id
    if admin.id is None:
        return None
    db_admin = await db.get(Admin, admin.id)
    if db_admin is not None and db_admin.workspace_id is not None:
        return db_admin.workspace_id
    return None


async def require_admin_workspace_id(
    db: AsyncSession,
    admin: AdminDetails,
    is_owner: bool,
) -> int:
    workspace_id = await resolve_admin_workspace_id(db, admin, is_owner)
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace scope required",
        )
    return workspace_id


async def ensure_actor_panel_access(
    db: AsyncSession,
    panel: OCPanel | None,
    *,
    is_owner: bool,
    admin: AdminDetails | None,
    identity: str | None = None,
) -> None:
    """
    Authorize panel access for Owner, tenant staff, or customer purchaser identity.

    Tenant staff must match tenant_id and workspace_id. Cross-workspace access returns 404.
    """
    if panel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")
    if is_owner:
        return
    if admin is not None:
        from app.services.tenant_admin_scope import require_admin_tenant_id

        tenant_id = await require_admin_tenant_id(db, admin, is_owner)
        if panel.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")
        workspace_id = await require_admin_workspace_id(db, admin, is_owner)
        if panel.workspace_id is None or panel.workspace_id != workspace_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")
        return
    if identity is not None and panel.purchaser_identity == identity:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this panel")


async def ensure_actor_admin_workspace_access(
    db: AsyncSession,
    db_admin: Admin,
    actor: AdminDetails,
    is_owner: bool,
) -> None:
    """Reject cross-workspace admin access for tenant staff (404 to avoid leaking existence)."""
    if is_owner or actor.is_owner:
        return
    from app.services.tenant_admin_scope import resolve_admin_tenant_id

    tenant_id = await resolve_admin_tenant_id(db, actor, is_owner)
    if tenant_id is None or db_admin.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin not found")
    workspace_id = await require_admin_workspace_id(db, actor, is_owner)
    if db_admin.workspace_id is None or db_admin.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin not found")


async def user_belongs_to_actor_workspace(
    db_user: User | None,
    actor_workspace_id: int,
) -> bool:
    if db_user is None:
        return False
    return db_user.workspace_id == actor_workspace_id


async def resolve_tenant_workspace_scope(
    db: AsyncSession,
    admin: AdminDetails,
) -> tuple[int | None, int | None]:
    """
    Return (tenant_id, workspace_id) for tenant-scoped resource queries.

    Owner receives (None, None) for global visibility. Tenant staff receive a required workspace.
    """
    if admin.is_owner:
        return None, None
    from app.services.tenant_admin_scope import resolve_admin_tenant_id

    tenant_id = await resolve_admin_tenant_id(db, admin, False)
    workspace_id = await require_admin_workspace_id(db, admin, False)
    return tenant_id, workspace_id


async def workspace_id_for_binding_admin(
    db: AsyncSession,
    tenant_id: int,
    binding_admin_id: int | None,
) -> int | None:
    """Resolve workspace for a Telegram connection binding (NULL binding = Owner/platform)."""
    if binding_admin_id is None:
        return None
    db_admin = await db.get(Admin, binding_admin_id)
    if db_admin is None or db_admin.tenant_id != tenant_id:
        return None
    return db_admin.workspace_id
