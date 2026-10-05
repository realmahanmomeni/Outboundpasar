"""Outbound Center integration HTTP client with structured errors."""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
from fastapi import HTTPException, status

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCIntegration
from app.utils.crypto import decrypt_secret

logger = logging.getLogger(__name__)


class OcIntegrationApiError(Exception):
    """Raised when Outbound Center returns a non-success HTTP status."""

    def __init__(self, *, oc_status: int, detail: str):
        self.oc_status = oc_status
        self.detail = detail
        super().__init__(detail)

    @property
    def code(self) -> str | None:
        """Machine code from OC (e.g. ``PANEL_INACTIVE``), if the detail carries one."""
        match = _OC_CODE_RE.search(self.detail or "")
        return match.group(0) if match else None


class OcConnectionTokenMissing(Exception):
    """No usable per-connection OC credential (legacy connection, no tenant, wrong account)."""


_OC_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

# Codes returned by OC's scoped integration API -> (PG status, message)
_OC_CODE_MAP: dict[str, tuple[int, str]] = {
    "CONNECTION_TOKEN_REQUIRED": (409, "Outbound Center connection credential missing. Reconnect Telegram."),
    "CONNECTION_TOKEN_INVALID": (409, "Outbound Center rejected the connection credential. Reconnect Telegram."),
    "CONNECTION_REVOKED": (409, "This Telegram connection was revoked on Outbound Center. Reconnect Telegram."),
    "ACCOUNT_MISMATCH": (403, "Panel does not belong to the connected Outbound Center account."),
    "ACCOUNT_BANNED": (403, "The connected Outbound Center account is not allowed to use the integration."),
    "PANEL_NOT_FOUND": (404, "Panel not found on Outbound Center for the connected account."),
    "PANEL_INACTIVE": (409, "The panel is not active on Outbound Center."),
    "PANEL_CREDENTIALS_MISSING": (409, "The panel has no sub-admin credentials on Outbound Center."),
    "PANEL_TYPE_UNSUPPORTED": (502, "The panel type does not support the integration."),
    "PANEL_UNAVAILABLE": (503, "The panel is unreachable from Outbound Center."),
    "PANEL_AUTH_FAILED": (502, "Outbound Center could not authenticate to the panel."),
    "PANEL_FORBIDDEN": (502, "The panel denied the operation for the sub-admin."),
    "USER_NOT_FOUND": (404, "User not found on the Outbound Center panel."),
    "GROUP_NOT_ALLOWED": (400, "One or more groups are not available on this panel."),
    "CONFIG_NOT_ALLOWED": (400, "One or more configs are not available on this panel."),
    "CONFIG_NOT_IN_GROUPS": (400, "A selected config is not part of the selected groups."),
}


def http_exception_from_oc_error(err: OcIntegrationApiError) -> HTTPException:
    detail_lower = (err.detail or "").lower()

    if err.oc_status == status.HTTP_500_INTERNAL_SERVER_ERROR and "integration api is not configured" in detail_lower:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Outbound Center integration API is not configured. "
                "Set INTEGRATION_API_TOKEN on Outbound Center to match PasarGuard's integration token."
            ),
        )

    if err.oc_status in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        if "integration token" in detail_lower or "x-integration-token" in detail_lower:
            return HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Outbound Center rejected the integration token. Verify tokens match on both sides.",
            )

    code = err.code
    if code in _OC_CODE_MAP:
        http_status, message = _OC_CODE_MAP[code]
        return HTTPException(status_code=http_status, detail=message)

    if "invalid verification code" in detail_lower:
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification code")

    if "verification code expired" in detail_lower or "connection request expired" in detail_lower:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Verification session expired. Start Connect Telegram again.",
        )

    if "no code issued" in detail_lower:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Complete the Telegram bot steps first, then enter your verification code.",
        )

    if "too many attempts" in detail_lower:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many verification attempts. Start Connect Telegram again.",
        )

    if "unknown connection request" in detail_lower:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connection session not found. Start Connect Telegram again.",
        )

    if err.oc_status == status.HTTP_404_NOT_FOUND:
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Outbound Center integration endpoint error")

    if err.oc_status == status.HTTP_502_BAD_GATEWAY:
        if "<!doctype html>" in detail_lower or "bad gateway" in detail_lower:
            return HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Outbound Center could not reach the remote panel API while creating the test user. "
                    "Verify the panel is online and sub-admin credentials are valid on Outbound Center."
                ),
            )

    if err.oc_status >= 500:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Outbound Center is temporarily unavailable. Try again later.",
        )

    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=err.detail or "Outbound Center integration error")


async def call_oc_api(
    integration: OCIntegration,
    method: str,
    path: str,
    json: dict | None = None,
    *,
    connection_token: str | None = None,
) -> Any:
    """Call OC. ``connection_token`` (decrypted) scopes the call to one connected account."""
    decrypted_token = await decrypt_secret(integration.api_token_encrypted)
    url = f"{integration.base_url.rstrip('/')}{path}"
    headers = {"X-Integration-Token": decrypted_token}
    if connection_token:
        headers["X-OC-Connection-Token"] = connection_token

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                headers=headers,
                json=json,
                timeout=aiohttp.ClientTimeout(total=30.0),
            ) as resp:
                if resp.status >= 400:
                    detail = await _read_oc_error_detail(resp)
                    logger.error("OC API error %s %s: %s", method, resp.status, detail[:500])
                    raise OcIntegrationApiError(oc_status=resp.status, detail=detail)
                if resp.status != 204:
                    return await resp.json()
                return None
    except OcIntegrationApiError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("OC API connection error %s %s: %s", method, url, e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Outbound Center unavailable") from e


async def _read_oc_error_detail(resp: aiohttp.ClientResponse) -> str:
    try:
        payload = await resp.json()
        if isinstance(payload, dict):
            raw = payload.get("detail", payload)
            if isinstance(raw, list):
                return str(raw)
            return str(raw)
    except Exception:
        pass
    text = await resp.text()
    return text[:2000] if text else f"HTTP {resp.status}"


async def get_active_integration_or_none(db: AsyncSession) -> OCIntegration | None:
    """
    The integration used for new connections/calls.

    Deterministic: the lowest-id active row (``ORDER BY id``), which is also what an
    unordered ``LIMIT 1`` returned in practice. Extra active rows are never deleted or
    modified; a warning is logged so the ambiguity is visible.
    """
    rows = (
        await db.execute(
            select(OCIntegration).where(OCIntegration.is_active.is_(True)).order_by(OCIntegration.id.asc()).limit(2)
        )
    ).scalars().all()
    if len(rows) > 1:
        logger.warning(
            "Multiple active OC integrations (ids %s, ...): using id=%s. Deactivate the others "
            "to remove the ambiguity.",
            rows[0].id,
            rows[0].id,
        )
    return rows[0] if rows else None


async def get_active_integration(db: AsyncSession) -> OCIntegration:
    integration = await get_active_integration_or_none(db)
    if integration is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No active OC integration")
    return integration
