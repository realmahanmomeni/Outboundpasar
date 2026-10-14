from __future__ import annotations

from datetime import UTC, datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ManagedSubdomainBotBinding,
    ManagedSubdomainBotBindingStatus,
    Workspace,
)


async def get_binding_by_idempotency_key(
    db: AsyncSession, idempotency_key: str
) -> ManagedSubdomainBotBinding | None:
    return (
        await db.execute(
            select(ManagedSubdomainBotBinding).where(
                ManagedSubdomainBotBinding.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()


async def get_binding_by_label(db: AsyncSession, label: str) -> ManagedSubdomainBotBinding | None:
    return (
        await db.execute(
            select(ManagedSubdomainBotBinding).where(ManagedSubdomainBotBinding.label == label)
        )
    ).scalar_one_or_none()


async def get_binding_for_claim(
    db: AsyncSession,
    *,
    workspace_id: int,
    binding_admin_id: int,
    label: str,
) -> ManagedSubdomainBotBinding | None:
    return (
        await db.execute(
            select(ManagedSubdomainBotBinding).where(
                ManagedSubdomainBotBinding.workspace_id == workspace_id,
                ManagedSubdomainBotBinding.binding_admin_id == binding_admin_id,
                ManagedSubdomainBotBinding.label == label,
                ManagedSubdomainBotBinding.status == ManagedSubdomainBotBindingStatus.pending_claim.value,
                ManagedSubdomainBotBinding.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def get_pending_binding_for_workspace_admin(
    db: AsyncSession,
    *,
    workspace_id: int,
    binding_admin_id: int,
) -> ManagedSubdomainBotBinding | None:
    return (
        await db.execute(
            select(ManagedSubdomainBotBinding).where(
                ManagedSubdomainBotBinding.workspace_id == workspace_id,
                ManagedSubdomainBotBinding.binding_admin_id == binding_admin_id,
                ManagedSubdomainBotBinding.status == ManagedSubdomainBotBindingStatus.pending_claim.value,
                ManagedSubdomainBotBinding.revoked_at.is_(None),
            )
            .order_by(ManagedSubdomainBotBinding.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_claimed_binding_for_workspace(
    db: AsyncSession, workspace_id: int
) -> ManagedSubdomainBotBinding | None:
    return (
        await db.execute(
            select(ManagedSubdomainBotBinding).where(
                ManagedSubdomainBotBinding.workspace_id == workspace_id,
                ManagedSubdomainBotBinding.status == ManagedSubdomainBotBindingStatus.claimed.value,
                ManagedSubdomainBotBinding.revoked_at.is_(None),
            )
            .order_by(ManagedSubdomainBotBinding.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def upsert_bot_binding(
    db: AsyncSession,
    *,
    tenant_id: int,
    workspace_id: int,
    binding_admin_id: int,
    label: str,
    fqdn: str,
    oc_subdomain_id: str,
    oc_account_id: int,
    idempotency_key: str | None,
) -> ManagedSubdomainBotBinding:
    if idempotency_key:
        existing = await get_binding_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            return existing

    locked = (
        await db.execute(
            select(ManagedSubdomainBotBinding)
            .where(ManagedSubdomainBotBinding.label == label)
            .with_for_update()
        )
    ).scalar_one_or_none()
    by_label = locked
    if by_label is not None:
        if by_label.status == ManagedSubdomainBotBindingStatus.revoked.value:
            if by_label.workspace_id != workspace_id:
                raise ValueError("label_conflict")
        elif by_label.workspace_id != workspace_id:
            raise ValueError("label_conflict")
        by_label.tenant_id = tenant_id
        by_label.binding_admin_id = binding_admin_id
        by_label.fqdn = fqdn
        by_label.oc_subdomain_id = oc_subdomain_id
        by_label.oc_account_id = oc_account_id
        by_label.status = ManagedSubdomainBotBindingStatus.pending_claim.value
        by_label.revoked_at = None
        by_label.idempotency_key = idempotency_key or by_label.idempotency_key
        await db.flush()
        return by_label

    row = ManagedSubdomainBotBinding(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        binding_admin_id=binding_admin_id,
        label=label,
        fqdn=fqdn,
        oc_subdomain_id=oc_subdomain_id,
        oc_account_id=oc_account_id,
        idempotency_key=idempotency_key,
        status=ManagedSubdomainBotBindingStatus.pending_claim.value,
    )
    db.add(row)
    await db.flush()
    return row


async def revoke_bot_binding(
    db: AsyncSession,
    binding: ManagedSubdomainBotBinding,
) -> None:
    binding.status = ManagedSubdomainBotBindingStatus.revoked.value
    binding.revoked_at = dt.now(UTC)
    await db.flush()


async def mark_binding_claimed(db: AsyncSession, binding: ManagedSubdomainBotBinding) -> None:
    binding.status = ManagedSubdomainBotBindingStatus.claimed.value
    binding.revoked_at = None
    await db.flush()


async def workspace_belongs_to_tenant(db: AsyncSession, workspace_id: int, tenant_id: int) -> bool:
    ws = await db.get(Workspace, workspace_id)
    return ws is not None and ws.tenant_id == tenant_id
