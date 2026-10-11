"""Buyer sub-admin credentials for direct Rebecca panel operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel
from app.services.oc_integration_client import OcIntegrationApiError, call_oc_api
from app.utils.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

_BUYER_AUTH_PATHS = (
    "/v1/integration/panels/{subscription_id}/buyer-sub-admin",
    "/v1/integration/panels/{subscription_id}/sub-admin-credentials",
)


@dataclass(frozen=True)
class BuyerPanelAuth:
    panel_url: str
    username: str
    password: str


def normalize_panel_type(panel_type: str | None) -> str:
    return (panel_type or "").strip().upper()


def panel_uses_local_rebecca_ops(panel: OCPanel) -> bool:
    return normalize_panel_type(panel.panel_type) == "REBECCA"


async def resolve_buyer_auth(db: AsyncSession, panel: OCPanel) -> BuyerPanelAuth | None:
    url = (panel.buyer_panel_url or "").strip()
    username = (panel.buyer_sub_admin_username or "").strip()
    encrypted = (panel.buyer_sub_admin_password_encrypted or "").strip()
    if not url or not username or not encrypted:
        return None
    try:
        password = await decrypt_secret(encrypted)
    except Exception:
        logger.warning("oc_buyer_auth_decrypt_failed panel_id=%s", panel.id)
        return None
    return BuyerPanelAuth(panel_url=url, username=username, password=password)


async def store_buyer_auth(
    db: AsyncSession,
    panel: OCPanel,
    auth: BuyerPanelAuth,
) -> None:
    panel.buyer_panel_url = auth.panel_url.rstrip("/")
    panel.buyer_sub_admin_username = auth.username
    panel.buyer_sub_admin_password_encrypted = await encrypt_secret(auth.password)


async def try_refresh_buyer_auth_from_oc(
    db: AsyncSession,
    panel: OCPanel,
    *,
    connection_token: str,
) -> bool:
    """Fetch buyer credentials from OC when the integration API exposes them."""
    subscription_id = str(panel.source_panel_id).strip()
    if not subscription_id:
        return False
    for template in _BUYER_AUTH_PATHS:
        path = template.format(subscription_id=subscription_id)
        try:
            data = await call_oc_api(
                panel.integration,
                "GET",
                path,
                connection_token=connection_token,
            )
        except OcIntegrationApiError as exc:
            if exc.oc_status == 404:
                continue
            logger.info(
                "oc_buyer_auth_fetch_failed panel_id=%s path=%s status=%s",
                panel.id,
                path,
                exc.oc_status,
            )
            return False
        if not isinstance(data, dict):
            continue
        url = str(data.get("panel_url") or data.get("url") or "").strip()
        username = str(data.get("username") or data.get("sub_admin_username") or "").strip()
        password = str(data.get("password") or data.get("sub_admin_password") or "").strip()
        if url and username and password:
            await store_buyer_auth(db, panel, BuyerPanelAuth(url, username, password))
            await db.flush()
            logger.info("oc_buyer_auth_stored panel_id=%s source_panel_id=%s", panel.id, subscription_id)
            return True
    return False
