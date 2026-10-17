"""Wizard discovery test-user provisioning with bounded USER_IDENTITY_BLOCKED recovery."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel
from app.services.oc_connection_credentials import call_oc_panel_api
from app.services.oc_integration_client import OcIntegrationApiError

logger = logging.getLogger(__name__)

_USER_IDENTITY_BLOCKED = "USER_IDENTITY_BLOCKED"


def _require_external_user_id(data: Any, *, operation: str) -> str:
    """OC user routes must return a non-empty ``external_user_id`` (PutUserResponse / test-user)."""
    if not isinstance(data, dict):
        raise OcIntegrationApiError(
            oc_status=502,
            detail=f"UPSTREAM_ERROR missing external_user_id after {operation}",
        )
    ext = str(data.get("external_user_id") or "").strip()
    if not ext:
        raise OcIntegrationApiError(
            oc_status=502,
            detail=f"UPSTREAM_ERROR missing external_user_id after {operation}",
        )
    return ext


async def provision_wizard_test_user(db: AsyncSession, panel: OCPanel) -> dict[str, Any]:
    """
    Create or reuse the panel discovery test user via Outbound Center.

    On ``USER_IDENTITY_BLOCKED`` from the canonical ``test-user`` route (Rebecca phantom
    reservation on the deterministic discovery VPN username), retry once using the
    existing user PUT API with a fresh ``external_user_id`` — same strategy as sync
    identity rotation in ``process_oc_sync``.
    """
    existing = (panel.test_user_id or "").strip()
    if existing:
        return {"external_user_id": existing}

    subscription_id = str(panel.source_panel_id).strip()
    test_path = f"/v1/integration/panels/{subscription_id}/test-user"
    try:
        data = await call_oc_panel_api(db, panel, "POST", test_path)
        return {"external_user_id": _require_external_user_id(data, operation="test-user POST")}
    except OcIntegrationApiError as exc:
        if exc.code != _USER_IDENTITY_BLOCKED:
            raise
        rotated_external_id = str(uuid.uuid4())
        put_path = f"/v1/integration/panels/{subscription_id}/users/{rotated_external_id}"
        logger.warning(
            "oc_wizard_test_user_identity_rotation panel_id=%s source_panel_id=%s",
            panel.id,
            subscription_id,
        )
        try:
            data = await call_oc_panel_api(
                db,
                panel,
                "PUT",
                put_path,
                {"groups": [], "configs": []},
            )
        except OcIntegrationApiError:
            raise
        return {
            "external_user_id": _require_external_user_id(
                data, operation="identity-rotation PUT"
            )
        }
