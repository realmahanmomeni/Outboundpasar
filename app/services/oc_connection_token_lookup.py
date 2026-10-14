"""Resolve Outbound Center connection tokens to trusted PasarGuard identity."""
from __future__ import annotations

import hashlib

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, Workspace
from app.db.models_oc import TenantTelegramConnection, TenantTelegramConnectionStatus


def connection_token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def store_connection_token_digest(connection: TenantTelegramConnection, plaintext_token: str) -> None:
    connection.oc_connection_token_sha256 = connection_token_sha256(plaintext_token)


async def resolve_active_connection_by_token(
    db: AsyncSession,
    connection_token: str,
) -> TenantTelegramConnection:
    digest = connection_token_sha256(connection_token.strip())
    row = await db.scalar(
        select(TenantTelegramConnection).where(
            TenantTelegramConnection.oc_connection_token_sha256 == digest,
            TenantTelegramConnection.status == TenantTelegramConnectionStatus.active.value,
            TenantTelegramConnection.active.is_(True),
            TenantTelegramConnection.revoked_at.is_(None),
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or inactive Outbound Center connection token",
        )
    if row.oc_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Outbound Center connection is missing account identity",
        )
    return row


async def pasarguard_identity_for_connection(
    db: AsyncSession,
    connection: TenantTelegramConnection,
) -> tuple[int, int, int]:
    """Return (tenant_id, workspace_id, binding_admin_id) derived server-side."""
    tenant_id = int(connection.tenant_id)
    if connection.binding_admin_id is not None:
        admin = await db.get(Admin, connection.binding_admin_id)
        if admin is None or admin.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid connection binding")
        workspace = await db.scalar(
            select(Workspace).where(Workspace.owner_admin_id == admin.id, Workspace.tenant_id == tenant_id)
        )
        if workspace is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No workspace for connection binding")
        return tenant_id, int(workspace.id), int(admin.id)

    workspace = await db.scalar(select(Workspace).where(Workspace.tenant_id == tenant_id).limit(1))
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No workspace for tenant connection")
    return tenant_id, int(workspace.id), int(workspace.owner_admin_id)
