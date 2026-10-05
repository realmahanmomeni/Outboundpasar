"""Resolve tenant scope for Outbound Center integration APIs."""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, Tenant, TenantStatus
from app.models.admin import AdminDetails
from app.services.tenant_admin_scope import resolve_admin_tenant_id


async def resolve_integration_tenant_id(
    db: AsyncSession,
    admin: AdminDetails,
    is_owner: bool,
    *,
    auto_provision: bool = False,
) -> int | None:
    """
    Return the tenant id used for TenantTelegramConnection and imported panels.

    Tenant admins must have tenant_id on their account. Platform owners may lack
    tenant_id until their first integration action; auto_provision creates a
    dedicated tenant and persists it on the admin record.
    """
    if not is_owner:
        tenant_id = await resolve_admin_tenant_id(db, admin, is_owner=False)
        if tenant_id is not None:
            return tenant_id
        if auto_provision:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Admin is not assigned to a tenant",
            )
        return None

    if admin.tenant_id is not None:
        return admin.tenant_id

    db_admin = await db.get(Admin, admin.id)
    if db_admin is not None and db_admin.tenant_id is not None:
        return db_admin.tenant_id

    if not auto_provision:
        return None

    tenant = Tenant(name=f"Integration ({admin.username})", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    if db_admin is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    db_admin.tenant_id = tenant.id
    await db.flush()
    return tenant.id
