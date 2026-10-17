"""Resolve Outbound Center Telegram connections for the authenticated actor."""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Workspace
from app.db.models_oc import TenantTelegramConnection
from app.models.admin import AdminDetails
from app.services.oc_telegram_connection import get_active_connection
from app.services.tenant_admin_scope import is_operator_admin_details


def binding_admin_id_for_actor(is_owner: bool, admin: AdminDetails) -> int | None:
    """
    Platform owners use the tenant-level (NULL) binding; tenant staff use their admin id.

    Operators inherit the workspace administrator's Telegram binding; use
    ``resolve_telegram_binding_admin_id`` when the actor may be an operator.
    """
    if is_owner:
        return None
    if admin.id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return admin.id


async def resolve_telegram_binding_admin_id(
    db: AsyncSession,
    admin: AdminDetails,
    is_owner: bool,
) -> int | None:
    """Resolve OC Telegram ``binding_admin_id`` for the actor (operators use workspace owner)."""
    if is_owner:
        return None
    if admin.id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    if is_operator_admin_details(admin):
        from app.services.workspace_scope import resolve_admin_workspace_id

        workspace_id = await resolve_admin_workspace_id(db, admin, False)
        if workspace_id is not None:
            workspace = await db.get(Workspace, workspace_id)
            if workspace is not None:
                return workspace.owner_admin_id
    return admin.id


async def resolve_oc_telegram_connection_for_actor(
    db: AsyncSession,
    admin: AdminDetails,
    is_owner: bool,
    tenant_id: int,
) -> TenantTelegramConnection | None:
    binding_admin_id = await resolve_telegram_binding_admin_id(db, admin, is_owner)
    return await get_active_connection(db, tenant_id, binding_admin_id=binding_admin_id)


async def require_oc_telegram_connection_for_actor(
    db: AsyncSession,
    admin: AdminDetails,
    is_owner: bool,
    tenant_id: int,
) -> TenantTelegramConnection:
    connection = await resolve_oc_telegram_connection_for_actor(db, admin, is_owner, tenant_id)
    if connection is None or connection.oc_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Telegram connection required for this tenant",
        )
    return connection
