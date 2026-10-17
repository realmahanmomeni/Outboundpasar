"""Apply Outbound Center panel metadata to a local OCPanel row."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel
from app.services.oc_panel_buyer_auth import BuyerPanelAuth, normalize_panel_type, store_buyer_auth


async def apply_panel_metadata_from_oc(db: AsyncSession, panel: OCPanel, meta: dict[str, Any] | None) -> None:
    if not meta:
        return
    panel_type = meta.get("panel_type")
    if panel_type:
        panel.panel_type = normalize_panel_type(str(panel_type))
    url = str(meta.get("panel_url") or meta.get("buyer_panel_url") or "").strip()
    username = str(meta.get("sub_admin_username") or meta.get("buyer_sub_admin_username") or "").strip()
    password = str(meta.get("sub_admin_password") or meta.get("buyer_sub_admin_password") or "").strip()
    if url and username and password:
        await store_buyer_auth(db, panel, BuyerPanelAuth(url, username, password))
