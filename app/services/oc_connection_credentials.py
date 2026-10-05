"""Per-connection Outbound Center credentials for scoped panel/user calls.

Every panel/user call must carry the ``X-OC-Connection-Token`` that OC issued when the
tenant's Telegram connection was verified. OC derives the account from the token, so a
tenant can only ever reach panels owned by the account it proved control of.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Workspace
from app.db.models_oc import OCPanel, TenantTelegramConnection, TenantTelegramConnectionStatus
from app.models.admin import AdminDetails
from app.services.oc_actor_connection import resolve_telegram_binding_admin_id
from app.services.oc_integration_client import OcConnectionTokenMissing, call_oc_api
from app.services.oc_telegram_connection import _binding_filter, get_active_connection
from app.utils.crypto import decrypt_secret

RECONNECT_HINT = "Reconnect Telegram to refresh the Outbound Center credential."
_BINDING_UNSET = object()


async def encrypt_connection_token(token: str) -> str:
    from app.utils.crypto import encrypt_secret

    return await encrypt_secret(token)


async def _decrypt(row: TenantTelegramConnection) -> str:
    if not row.oc_connection_token_encrypted:
        raise OcConnectionTokenMissing(f"Telegram connection {row.id} has no Outbound Center credential. {RECONNECT_HINT}")
    return await decrypt_secret(row.oc_connection_token_encrypted)


async def get_connection_for_tenant(
    db: AsyncSession,
    tenant_id: int,
    *,
    oc_account_id: int | None = None,
    allow_revoked: bool = False,
    binding_admin_id: int | None | object = _BINDING_UNSET,
) -> TenantTelegramConnection | None:
    """Active connection; with ``allow_revoked`` fall back to the newest one holding a token."""
    if oc_account_id is not None:
        stmt = select(TenantTelegramConnection).where(
            TenantTelegramConnection.tenant_id == tenant_id,
            TenantTelegramConnection.status == TenantTelegramConnectionStatus.active.value,
            TenantTelegramConnection.active.is_(True),
            TenantTelegramConnection.revoked_at.is_(None),
            TenantTelegramConnection.oc_account_id == oc_account_id,
        )
        if binding_admin_id is not _BINDING_UNSET:
            resolved_binding = None if binding_admin_id is None else int(binding_admin_id)
            stmt = _binding_filter(stmt, resolved_binding)
        active = await db.scalar(stmt)
        if active is not None:
            return active
    if binding_admin_id is not _BINDING_UNSET:
        resolved_binding = None if binding_admin_id is None else int(binding_admin_id)
        active = await get_active_connection(db, tenant_id, binding_admin_id=resolved_binding)
        if active is not None and (oc_account_id is None or active.oc_account_id == oc_account_id):
            return active
    elif oc_account_id is None:
        active = await get_active_connection(db, tenant_id, binding_admin_id=None)
        if active is not None:
            return active
    if not allow_revoked:
        return None
    stmt = select(TenantTelegramConnection).where(
        TenantTelegramConnection.tenant_id == tenant_id,
        TenantTelegramConnection.oc_connection_token_encrypted.is_not(None),
    )
    if oc_account_id is not None:
        stmt = stmt.where(TenantTelegramConnection.oc_account_id == oc_account_id)
    if binding_admin_id is not _BINDING_UNSET:
        if binding_admin_id is None:
            stmt = stmt.where(TenantTelegramConnection.binding_admin_id.is_(None))
        else:
            stmt = stmt.where(TenantTelegramConnection.binding_admin_id == binding_admin_id)
    return (await db.scalars(stmt.order_by(TenantTelegramConnection.id.desc()).limit(1))).first()


async def connection_token_for_actor(
    db: AsyncSession,
    admin: AdminDetails,
    is_owner: bool,
    tenant_id: int,
    *,
    oc_account_id: int | None = None,
    allow_revoked: bool = False,
) -> str:
    binding = await resolve_telegram_binding_admin_id(db, admin, is_owner)
    row = await get_connection_for_tenant(
        db,
        tenant_id,
        oc_account_id=oc_account_id,
        allow_revoked=allow_revoked,
        binding_admin_id=binding,
    )
    if row is None:
        raise OcConnectionTokenMissing("No active Telegram connection for this tenant/account.")
    return await _decrypt(row)


async def connection_token_for_tenant(
    db: AsyncSession,
    tenant_id: int | None,
    *,
    oc_account_id: int | None = None,
    allow_revoked: bool = False,
) -> str | None:
    """Decrypted connection token, or ``None`` for tenant-less (legacy) panels.

    A tenant-less panel has no connection to scope by; the call is sent without a
    connection token and Outbound Center refuses it (``CONNECTION_TOKEN_REQUIRED``).
    """
    if tenant_id is None:
        return None
    row = await get_connection_for_tenant(
        db, tenant_id, oc_account_id=oc_account_id, allow_revoked=allow_revoked
    )
    if row is None:
        raise OcConnectionTokenMissing("No active Telegram connection for this tenant/account.")
    return await _decrypt(row)


async def binding_admin_id_for_panel(
    db: AsyncSession, panel: OCPanel
) -> int | None | object:
    """Workspace owner admin id for panel-scoped OC credentials; UNSET if unknown."""
    if panel.workspace_id is None:
        return _BINDING_UNSET
    workspace = await db.get(Workspace, panel.workspace_id)
    if workspace is None:
        return _BINDING_UNSET
    return workspace.owner_admin_id


async def panel_has_active_connection(db: AsyncSession, panel: OCPanel) -> bool:
    """True when the panel's workspace-scoped Telegram OC binding is active."""
    if panel.tenant_id is None:
        return True
    binding = await binding_admin_id_for_panel(db, panel)
    row = await get_connection_for_tenant(
        db,
        panel.tenant_id,
        oc_account_id=panel.oc_account_id,
        binding_admin_id=binding,
    )
    return row is not None


async def connection_token_for_panel(
    db: AsyncSession, panel: OCPanel, *, allow_revoked: bool = False
) -> str | None:
    if panel.tenant_id is None:
        return None
    binding = await binding_admin_id_for_panel(db, panel)
    row = await get_connection_for_tenant(
        db,
        panel.tenant_id,
        oc_account_id=panel.oc_account_id,
        allow_revoked=allow_revoked,
        binding_admin_id=binding,
    )
    if row is None:
        raise OcConnectionTokenMissing("No active Telegram connection for this tenant/account.")
    return await _decrypt(row)


async def connection_token_by_id(db: AsyncSession, connection_id: int) -> str:
    row = await db.get(TenantTelegramConnection, connection_id)
    if row is None:
        raise OcConnectionTokenMissing(f"Telegram connection {connection_id} no longer exists.")
    return await _decrypt(row)


async def call_oc_panel_api(
    db: AsyncSession,
    panel: OCPanel,
    method: str,
    path: str,
    json: dict | None = None,
    *,
    allow_revoked: bool = False,
) -> Any:
    """Account-scoped OC call for an imported panel (integration must be loaded)."""
    token = await connection_token_for_panel(db, panel, allow_revoked=allow_revoked)
    return await call_oc_api(panel.integration, method, path, json, connection_token=token)
