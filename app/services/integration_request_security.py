"""HMAC signing and replay protection for integration bot APIs."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime as dt, timedelta as td

from fastapi import HTTPException, Request, status
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from config import custom_domain_settings, runtime_settings


def _body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def build_signature_payload(timestamp: str, method: str, path: str, body: bytes) -> str:
    return f"{timestamp}\n{method.upper()}\n{path}\n{_body_sha256(body)}"


def compute_request_signature(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


async def consume_nonce(db: AsyncSession, nonce: str) -> None:
    from app.db.models import IntegrationApiNonce

    digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    now = dt.now(UTC)
    await db.execute(delete(IntegrationApiNonce).where(IntegrationApiNonce.expires_at < now))
    existing = await db.get(IntegrationApiNonce, digest)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Replayed request nonce")
    ttl = max(60, custom_domain_settings.managed_subdomain_bot_nonce_ttl_seconds)
    db.add(IntegrationApiNonce(nonce_sha256=digest, expires_at=now + td(seconds=ttl)))
    await db.flush()


async def verify_signed_integration_request(
    request: Request,
    db: AsyncSession,
    *,
    raw_body: bytes,
) -> None:
    secret = custom_domain_settings.managed_subdomain_bot_hmac_secret.strip()
    if not secret:
        if runtime_settings.testing:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Managed subdomain bot HMAC is not configured",
        )

    timestamp = request.headers.get("X-PG-Timestamp", "").strip()
    nonce = request.headers.get("X-PG-Nonce", "").strip()
    signature = request.headers.get("X-PG-Signature", "").strip()
    if not timestamp or not nonce or not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing request signature headers")

    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid timestamp") from None

    max_age = custom_domain_settings.managed_subdomain_bot_request_max_age_seconds
    now = int(dt.now(UTC).timestamp())
    if abs(now - ts) > max_age:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Request timestamp outside allowed window")

    payload = build_signature_payload(timestamp, request.method, request.url.path, raw_body)
    expected = compute_request_signature(secret, payload)
    if not secrets.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request signature")

    await consume_nonce(db, nonce)
