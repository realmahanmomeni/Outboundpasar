"""Tenant ↔ Outbound Center Telegram account binding."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime as dt

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import runtime_settings
from app.db.models_oc import TenantTelegramConnection, TenantTelegramConnectionStatus


def _bot_deep_link(intent_id: str) -> str:
    username = (getattr(runtime_settings, "oc_bot_username", None) or "").strip().lstrip("@")
    if not username:
        username = "outboundino_bot"
    return f"https://t.me/{username}?start=pgconnect_{intent_id}"


async def get_active_connection(db: AsyncSession, tenant_id: int) -> TenantTelegramConnection | None:
    return await db.scalar(
        select(TenantTelegramConnection).where(
            TenantTelegramConnection.tenant_id == tenant_id,
            TenantTelegramConnection.status == TenantTelegramConnectionStatus.active.value,
            TenantTelegramConnection.active.is_(True),
            TenantTelegramConnection.revoked_at.is_(None),
        )
    )


async def get_or_create_pending_connection(db: AsyncSession, tenant_id: int) -> TenantTelegramConnection:
    active = await get_active_connection(db, tenant_id)
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant already has an active Telegram connection. Revoke it before reconnecting.",
        )

    pending = await db.scalar(
        select(TenantTelegramConnection).where(
            TenantTelegramConnection.tenant_id == tenant_id,
            TenantTelegramConnection.status == TenantTelegramConnectionStatus.pending.value,
            TenantTelegramConnection.revoked_at.is_(None),
        )
    )
    intent_id = str(uuid.uuid4())
    if pending is None:
        pending = TenantTelegramConnection(
            tenant_id=tenant_id,
            status=TenantTelegramConnectionStatus.pending.value,
            pending_intent_id=intent_id,
        )
        db.add(pending)
    else:
        pending.pending_intent_id = intent_id
    await db.flush()
    return pending


async def start_connection(db: AsyncSession, tenant_id: int) -> dict:
    pending = await get_or_create_pending_connection(db, tenant_id)
    intent_id = pending.pending_intent_id
    if not intent_id:
        raise HTTPException(status_code=500, detail="Failed to create connection intent")
    return {
        "intent_id": intent_id,
        "bot_url": _bot_deep_link(intent_id),
        "status": pending.status,
    }


async def activate_connection(
    db: AsyncSession,
    tenant_id: int,
    *,
    telegram_user_id: int,
    oc_account_id: int,
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

    pending = await db.scalar(
        select(TenantTelegramConnection).where(
            TenantTelegramConnection.tenant_id == tenant_id,
            TenantTelegramConnection.status == TenantTelegramConnectionStatus.pending.value,
            TenantTelegramConnection.revoked_at.is_(None),
        )
    )
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
    await db.flush()
    return pending


async def revoke_connection(db: AsyncSession, tenant_id: int) -> None:
    row = await get_active_connection(db, tenant_id)
    if row is None:
        pending = await db.scalar(
            select(TenantTelegramConnection).where(
                TenantTelegramConnection.tenant_id == tenant_id,
                TenantTelegramConnection.status == TenantTelegramConnectionStatus.pending.value,
            )
        )
        if pending:
            pending.status = TenantTelegramConnectionStatus.revoked.value
            pending.active = False
            pending.revoked_at = dt.now(UTC)
        return
    row.status = TenantTelegramConnectionStatus.revoked.value
    row.active = False
    row.revoked_at = dt.now(UTC)

    from app.node.oc_sync import enqueue_tenant_panel_user_deletions

    await enqueue_tenant_panel_user_deletions(db, tenant_id)


async def require_active_telegram_for_tenant_admin(
    db: AsyncSession,
    admin: object,
    is_owner: bool,
) -> None:
    """Tenant admins need an active OC Telegram connection for panel integration actions."""
    if is_owner:
        return
    tenant_id = getattr(admin, "tenant_id", None)
    if tenant_id is None:
        return
    if await get_active_connection(db, tenant_id) is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Active Telegram connection required",
        )
