"""Execute Outbound Center integration panel paths locally (Rebecca)."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel
from app.services.oc_integration_client import OcIntegrationApiError
from app.services.oc_panel_buyer_auth import (
    BuyerPanelAuth,
    panel_uses_local_rebecca_ops,
    resolve_buyer_auth,
    try_refresh_buyer_auth_from_oc,
)
from app.services.oc_rebecca_integration import LocalIntegrationError, delete_external_user, get_external_user
from app.services.oc_rebecca_integration import get_panel_configs, get_panel_groups, provision_external_user
from app.services.oc_rebecca_integration import provision_test_user

_PANEL_PATH_RE = re.compile(
    r"^/v1/integration/panels/(?P<sub_id>[^/]+)(?:/(?P<rest>.*))?$"
)


def _parse_panel_path(path: str, source_panel_id: str) -> tuple[str, str | None] | None:
    raw = (path or "").split("?", 1)[0].strip()
    match = _PANEL_PATH_RE.match(raw)
    if not match:
        return None
    if str(match.group("sub_id")) != str(source_panel_id):
        return None
    return str(source_panel_id), match.group("rest")


async def _account_id(panel: OCPanel) -> int:
    if panel.oc_account_id is not None:
        return int(panel.oc_account_id)
    try:
        return int(panel.purchaser_identity)
    except (TypeError, ValueError):
        return 0


async def execute_local_panel_api(
    db: AsyncSession,
    panel: OCPanel,
    method: str,
    path: str,
    json: dict | None,
    auth: BuyerPanelAuth,
) -> Any:
    parsed = _parse_panel_path(path, panel.source_panel_id)
    if parsed is None:
        raise LocalIntegrationError(404, "Unknown integration path")
    subscription_id, rest = parsed
    sub_int = int(subscription_id)
    account_id = await _account_id(panel)
    rest = (rest or "").strip("/")

    if method == "GET" and rest == "groups":
        return {"groups": await get_panel_groups(auth)}
    if method == "GET" and rest == "configs":
        return {"configs": await get_panel_configs(auth)}
    if method == "POST" and rest == "test-user":
        ext = await provision_test_user(auth, account_id=account_id, subscription_id=sub_int)
        return {"external_user_id": ext, "status": "active"}

    user_match = re.match(r"^users/(?P<ext>[^/]+)(?:/(?P<suffix>.*))?$", rest or "")
    if user_match:
        external_user_id = user_match.group("ext")
        suffix = (user_match.group("suffix") or "").strip("/")
        if method == "PUT" and not suffix:
            body = json or {}
            return await provision_external_user(
                auth,
                account_id=account_id,
                subscription_id=sub_int,
                external_user_id=external_user_id,
                groups=list(body.get("groups") or []),
                configs=list(body.get("configs") or []),
                proxy_settings=body.get("proxy_settings"),
            )
        if method == "GET" and not suffix:
            return await get_external_user(
                auth,
                account_id=account_id,
                subscription_id=sub_int,
                external_user_id=external_user_id,
            )
        if method == "DELETE" and not suffix:
            await delete_external_user(
                auth,
                account_id=account_id,
                subscription_id=sub_int,
                external_user_id=external_user_id,
            )
            return None

    raise LocalIntegrationError(404, "Unsupported local integration operation")


async def maybe_local_panel_api(
    db: AsyncSession,
    panel: OCPanel,
    method: str,
    path: str,
    json: dict | None,
    *,
    connection_token: str | None,
) -> Any | None:
    """Return a response when handled locally; None to fall through to OC HTTP."""
    if not panel_uses_local_rebecca_ops(panel):
        return None
    auth = await resolve_buyer_auth(db, panel)
    if auth is None and connection_token:
        if await try_refresh_buyer_auth_from_oc(db, panel, connection_token=connection_token):
            auth = await resolve_buyer_auth(db, panel)
    if auth is None:
        return None
    try:
        return await execute_local_panel_api(db, panel, method, path, json, auth)
    except LocalIntegrationError:
        raise


def local_integration_error_to_oc(err: LocalIntegrationError) -> OcIntegrationApiError:
    return OcIntegrationApiError(oc_status=err.http_status, detail=err.code or err.detail)
