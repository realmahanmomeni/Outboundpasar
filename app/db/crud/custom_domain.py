from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime as dt, timedelta as td

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState, Workspace


def hash_verification_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_verification_token() -> str:
    return secrets.token_urlsafe(32)


async def list_custom_domains_for_workspace(
    db: AsyncSession,
    workspace_id: int,
) -> list[SubscriptionCustomDomain]:
    return (
        (
            await db.execute(
                select(SubscriptionCustomDomain)
                .where(SubscriptionCustomDomain.workspace_id == workspace_id)
                .order_by(SubscriptionCustomDomain.id.asc())
            )
        )
        .scalars()
        .all()
    )


async def get_custom_domain_by_id(
    db: AsyncSession,
    domain_id: int,
    *,
    workspace_id: int | None = None,
) -> SubscriptionCustomDomain | None:
    stmt = select(SubscriptionCustomDomain).where(SubscriptionCustomDomain.id == domain_id)
    if workspace_id is not None:
        stmt = stmt.where(SubscriptionCustomDomain.workspace_id == workspace_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_custom_domain(
    db: AsyncSession,
    *,
    workspace_id: int,
    tenant_id: int,
    hostname_normalized: str,
) -> tuple[SubscriptionCustomDomain, str]:
    token = generate_verification_token()
    row = SubscriptionCustomDomain(
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        hostname_normalized=hostname_normalized,
        state=SubscriptionCustomDomainState.pending_verification,
        verification_token_hash=hash_verification_token(token),
        verification_expires_at=dt.now(UTC) + td(days=7),
    )
    db.add(row)
    await db.flush()
    return row, token


async def refresh_verification_token(db: AsyncSession, domain: SubscriptionCustomDomain) -> str:
    token = generate_verification_token()
    domain.verification_token_hash = hash_verification_token(token)
    domain.verification_expires_at = dt.now(UTC) + td(days=7)
    domain.state = SubscriptionCustomDomainState.pending_verification
    domain.verified_at = None
    await db.flush()
    return token


async def mark_domain_verified(db: AsyncSession, domain: SubscriptionCustomDomain) -> None:
    domain.state = SubscriptionCustomDomainState.verified
    domain.verified_at = dt.now(UTC)
    domain.verification_token_hash = None
    domain.verification_expires_at = None
    await db.flush()


async def set_domain_state(
    db: AsyncSession,
    domain: SubscriptionCustomDomain,
    state: SubscriptionCustomDomainState,
    *,
    error: str | None = None,
) -> None:
    domain.state = state
    if error is not None:
        domain.certificate_last_error = error[:512]
    await db.flush()


async def workspace_tenant_id(db: AsyncSession, workspace_id: int) -> int | None:
    ws = await db.get(Workspace, workspace_id)
    return ws.tenant_id if ws else None
