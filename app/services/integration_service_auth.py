"""Authenticate server-to-server calls from Outbound Center integration services."""
from __future__ import annotations

import secrets

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCIntegration
from app.services.oc_integration_client import get_active_integration
from app.utils.crypto import decrypt_secret


async def verify_active_integration_token(db: AsyncSession, x_integration_token: str | None) -> OCIntegration:
    if not x_integration_token or not x_integration_token.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Integration token required")
    integration = await get_active_integration(db)
    expected = await decrypt_secret(integration.api_token_encrypted)
    if not secrets.compare_digest(x_integration_token.strip(), expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid integration token")
    return integration
