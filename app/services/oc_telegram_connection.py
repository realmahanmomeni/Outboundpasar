"""Tenant ↔ Outbound Center Telegram account binding."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime as dt
from urllib.parse import urlparse

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import runtime_settings
from app.db.models_oc import TenantTelegramConnection, TenantTelegramConnectionStatus


_TRUSTED_BOT_HOSTS = {"t.me", "telegram.me"}


def _bot_deep_link(intent_id: str) -> str | None:
    """Fallback deep link from OC_BOT_USERNAME; ``None`` when it is not configured."""
    username = (getattr(runtime_settings, "oc_bot_username", None) or "").strip().lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}?start=pgconnect_{intent_id}"


def _is_trusted_bot_url(url: str, intent_id: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in _TRUSTED_BOT_HOSTS
        and intent_id in (parsed.query or "")
    )


def resolve_bot_url(intent_id: str, oc_bot_url: str | None = None) -> str:
    """
    Telegram deep link for an intent. Outbound Center knows its own bot, so its returned
    ``bot_url`` wins; ``OC_BOT_USERNAME`` is the fallback. No bot name is hardcoded.
    """
    candidate = (oc_bot_url or "").strip()
    if candidate and _is_trusted_bot_url(candidate, intent_id):
        return candidate
    fallback = _bot_deep_link(intent_id)
    if fallback is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Outbound Center did not return a bot link and OC_BOT_USERNAME is not configured."
            ),
        )
    return fallback


def bot_deep_link_for_intent(intent_id: str, stored_bot_url: str | None = None) -> str:
    return resolve_bot_url(intent_id, stored_bot_url)


def _binding_filter(stmt, binding_admin_id: int | None):
    if binding_admin_id is None:
        return stmt.where(TenantTelegramConnection.binding_admin_id.is_(None))
    return stmt.where(TenantTelegramConnection.binding_admin_id == binding_admin_id)


async def get_active_connection(
    db: AsyncSession,
    tenant_id: int,
    *,
    binding_admin_id: int | None = None,
) -> TenantTelegramConnection | None:
    stmt = select(TenantTelegramConnection).where(
        TenantTelegramConnection.tenant_id == tenant_id,
        TenantTelegramConnection.status == TenantTelegramConnectionStatus.active.value,
        TenantTelegramConnection.active.is_(True),
        TenantTelegramConnection.revoked_at.is_(None),
    )
    stmt = _binding_filter(stmt, binding_admin_id)
    return await db.scalar(stmt)


async def get_or_create_pending_connection(
    db: AsyncSession,
    tenant_id: int,
    *,
    binding_admin_id: int | None = None,
) -> TenantTelegramConnection:
    active = await get_active_connection(db, tenant_id, binding_admin_id=binding_admin_id)
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant already has an active Telegram connection. Revoke it before reconnecting.",
        )

    stmt = select(TenantTelegramConnection).where(
        TenantTelegramConnection.tenant_id == tenant_id,
        TenantTelegramConnection.status == TenantTelegramConnectionStatus.pending.value,
        TenantTelegramConnection.revoked_at.is_(None),
    )
    stmt = _binding_filter(stmt, binding_admin_id)
    pending = await db.scalar(stmt)
    intent_id = str(uuid.uuid4())
    if pending is None:
        pending = TenantTelegramConnection(
            tenant_id=tenant_id,
            binding_admin_id=binding_admin_id,
            status=TenantTelegramConnectionStatus.pending.value,
            pending_intent_id=intent_id,
        )
        db.add(pending)
    else:
        pending.pending_intent_id = intent_id
    await db.flush()
    return pending


async def start_connection(
    db: AsyncSession,
    tenant_id: int,
    *,
    binding_admin_id: int | None = None,
) -> dict:
    """Create/refresh the pending intent. The bot link is attached after OC registers it."""
    pending = await get_or_create_pending_connection(db, tenant_id, binding_admin_id=binding_admin_id)
    intent_id = pending.pending_intent_id
    if not intent_id:
        raise HTTPException(status_code=500, detail="Failed to create connection intent")
    pending.pending_bot_url = None
    return {"intent_id": intent_id, "status": pending.status}


def set_pending_bot_url(pending: TenantTelegramConnection, intent_id: str, oc_bot_url: str | None) -> str:
    url = resolve_bot_url(intent_id, oc_bot_url)
    pending.pending_bot_url = url
    return url


async def activate_connection(
    db: AsyncSession,
    tenant_id: int,
    *,
    telegram_user_id: int,
    oc_account_id: int,
    connection_token_encrypted: str | None = None,
    binding_admin_id: int | None = None,
) -> TenantTelegramConnection:
    other = await db.scalar(
        select(TenantTelegramConnection).where(
            TenantTelegramConnection.telegram_user_id == telegram_user_id,
            TenantTelegramConnection.status == TenantTelegramConnectionStatus.active.value,
            TenantTelegramConnection.tenant_id != tenant_id,
            TenantTelegramConnection.revoked_at.is_(None),
        )
    )
    if other is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Telegram account is already connected to another tenant.",
        )

    stmt = select(TenantTelegramConnection).where(
        TenantTelegramConnection.tenant_id == tenant_id,
        TenantTelegramConnection.status == TenantTelegramConnectionStatus.pending.value,
        TenantTelegramConnection.revoked_at.is_(None),
    )
    stmt = _binding_filter(stmt, binding_admin_id)
    pending = await db.scalar(stmt)
    if pending is None:
        raise HTTPException(status_code=400, detail="No pending connection for this tenant")

    now = dt.now(UTC)
    pending.telegram_user_id = telegram_user_id
    pending.oc_account_id = oc_account_id
    pending.status = TenantTelegramConnectionStatus.active.value
    pending.active = True
    pending.verified_at = now
    pending.connected_at = now
    pending.pending_intent_id = None
    pending.pending_bot_url = None
    if connection_token_encrypted:
        pending.oc_connection_token_encrypted = connection_token_encrypted
    await db.flush()
    return pending


async def revoke_connection(
    db: AsyncSession,
    tenant_id: int,
    *,
    binding_admin_id: int | None = None,
) -> None:
    row = await get_active_connection(db, tenant_id, binding_admin_id=binding_admin_id)
    if row is None:
        stmt = select(TenantTelegramConnection).where(
            TenantTelegramConnection.tenant_id == tenant_id,
            TenantTelegramConnection.status == TenantTelegramConnectionStatus.pending.value,
        )
        stmt = _binding_filter(stmt, binding_admin_id)
        pending = await db.scalar(stmt)
        if pending:
            pending.status = TenantTelegramConnectionStatus.revoked.value
            pending.active = False
            pending.revoked_at = dt.now(UTC)
        return
    row.status = TenantTelegramConnectionStatus.revoked.value
    row.active = False
    row.revoked_at = dt.now(UTC)

    from app.node.oc_sync import (
        enqueue_oc_connection_revoke,
        enqueue_tenant_panel_user_deletions,
        enqueue_workspace_panel_user_deletions,
    )
    from app.services.workspace_scope import workspace_id_for_binding_admin

    # Order matters: user deletions are queued first (they stay authorised with the revoked
    # token on OC), then the revoke itself is propagated to OC with retries.
    workspace_id = await workspace_id_for_binding_admin(db, tenant_id, binding_admin_id)
    if workspace_id is not None:
        await enqueue_workspace_panel_user_deletions(db, workspace_id, connection_id=row.id)
    else:
        await enqueue_tenant_panel_user_deletions(db, tenant_id, connection_id=row.id)
    await enqueue_oc_connection_revoke(db, row)


async def require_active_telegram_for_tenant_admin(
    db: AsyncSession,
    admin: object,
    is_owner: bool,
) -> None:
    """Tenant admins need an active OC Telegram connection for panel integration actions."""
    if is_owner:
        return
    from app.models.admin import AdminDetails
    from app.services.oc_actor_connection import require_oc_telegram_connection_for_actor
    from app.services.tenant_admin_scope import resolve_admin_tenant_id

    if not isinstance(admin, AdminDetails):
        return
    tenant_id = await resolve_admin_tenant_id(db, admin, is_owner)
    if tenant_id is None:
        return
    await require_oc_telegram_connection_for_actor(db, admin, is_owner, tenant_id)
