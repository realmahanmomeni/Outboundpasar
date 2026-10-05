"""Tenant scope resolution and tenant-administrator policy helpers."""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, AdminRole, Tenant, TenantStatus
from app.models.admin import AdminDetails

BUILTIN_ADMINISTRATOR_ROLE_ID = 2
BUILTIN_OPERATOR_ROLE_ID = 3


def is_builtin_administrator_role(role: AdminRole | None) -> bool:
    if role is None:
        return False
    return role.id == BUILTIN_ADMINISTRATOR_ROLE_ID or role.name == "administrator"


def is_builtin_operator_role(role: AdminRole | None) -> bool:
    if role is None:
        return False
    return role.id == BUILTIN_OPERATOR_ROLE_ID or role.name == "operator"


def is_tenant_administrator(admin: AdminDetails) -> bool:
    if admin.is_owner or admin.role is None:
        return False
    name = (admin.role.name or "").lower()
    return admin.role.id == BUILTIN_ADMINISTRATOR_ROLE_ID or name == "administrator"


def is_operator_admin_details(admin: AdminDetails) -> bool:
    """True when the authenticated actor is a tenant operator (not owner/administrator)."""
    if admin.is_owner or admin.role is None:
        return False
    name = (admin.role.name or "").lower()
    return admin.role.id == BUILTIN_OPERATOR_ROLE_ID or name == "operator"


async def resolve_admin_tenant_id(
    db: AsyncSession,
    admin: AdminDetails,
    is_owner: bool,
    *,
    persist_single_tenant_bind: bool = True,
) -> int | None:
    """
    Resolve the tenant id for a non-owner admin.

    When exactly one active tenant exists, an unscoped tenant admin/operator is
    bound to that tenant (persisted on the admin row) so legacy accounts created
    without tenant_id remain usable in single-tenant deployments.
    """
    if is_owner:
        return admin.tenant_id

    if admin.tenant_id is not None:
        return admin.tenant_id

    db_admin = await db.get(Admin, admin.id) if admin.id is not None else None
    if db_admin is not None and db_admin.tenant_id is not None:
        return db_admin.tenant_id

    if not persist_single_tenant_bind or db_admin is None:
        return None

    active_tenant_ids = (
        await db.execute(select(Tenant.id).where(Tenant.status == TenantStatus.active))
    ).scalars().all()
    if len(active_tenant_ids) != 1:
        return None

    db_admin.tenant_id = active_tenant_ids[0]
    await db.flush()
    return db_admin.tenant_id


async def require_admin_tenant_id(
    db: AsyncSession,
    admin: AdminDetails,
    is_owner: bool,
) -> int:
    tenant_id = await resolve_admin_tenant_id(db, admin, is_owner)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant scope required",
        )
    return tenant_id


async def get_admin_role(db: AsyncSession, role_id: int) -> AdminRole | None:
    return await db.get(AdminRole, role_id)


async def assert_tenant_admin_may_assign_role(
    db: AsyncSession,
    creator: AdminDetails,
    role_id: int,
) -> None:
    """Tenant administrators may create/promote only to the operator role."""
    if creator.is_owner:
        return

    role = await get_admin_role(db, role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    if role.is_owner or role_id == 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner role cannot be assigned via this endpoint.",
        )
    if is_builtin_administrator_role(role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant administrators cannot create or assign the administrator role.",
        )
    if not is_builtin_operator_role(role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant administrators may only create operator accounts.",
        )
