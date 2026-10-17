"""Explicit buyer-panel test-user provisioning (wizard-only; not remote OC)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel
from app.services.oc_connection_credentials import connection_token_for_panel
from app.services.oc_panel_buyer_auth import (
    panel_uses_local_rebecca_ops,
    resolve_buyer_auth,
    try_refresh_buyer_auth_from_oc,
)
from app.services.oc_panel_local_ops import execute_local_panel_api, local_integration_error_to_oc
from app.services.oc_rebecca_integration import LocalIntegrationError


async def provision_test_user_via_buyer_panel(db: AsyncSession, panel: OCPanel) -> dict[str, Any]:
    """
    Provision or reuse the discovery test user on the Rebecca buyer panel directly.

    Does not call Outbound Center integration HTTP. Intended for the import wizard when
    buyer sub-admin credentials are available.
    """
    if not panel_uses_local_rebecca_ops(panel):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Buyer-direct test user provisioning applies only to Rebecca panels.",
        )

    connection_token = await connection_token_for_panel(db, panel)
    auth = await resolve_buyer_auth(db, panel)
    if auth is None:
        if await try_refresh_buyer_auth_from_oc(db, panel, connection_token=connection_token):
            auth = await resolve_buyer_auth(db, panel)
    if auth is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Buyer sub-admin credentials are not available for this panel. "
                "Use the standard test-user endpoint or store credentials from Outbound Center."
            ),
        )

    path = f"/v1/integration/panels/{panel.source_panel_id}/test-user"
    try:
        return await execute_local_panel_api(db, panel, "POST", path, None, auth)
    except LocalIntegrationError as exc:
        raise HTTPException(
            status_code=exc.http_status if 400 <= exc.http_status < 600 else status.HTTP_502_BAD_GATEWAY,
            detail=exc.detail,
        ) from exc
