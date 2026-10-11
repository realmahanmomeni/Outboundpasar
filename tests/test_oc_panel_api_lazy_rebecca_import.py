"""PASARGUARD panel OC calls must not import Rebecca drivers (httpx) on the happy path."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_call_oc_panel_api_success_does_not_import_rebecca_stack():
    from app.db.models_oc import OCPanel, OCIntegration
    from app.services.oc_connection_credentials import call_oc_panel_api

    panel = OCPanel(
        integration_id=1,
        source_panel_id="1805",
        purchaser_identity="1314",
        oc_account_id=1314,
        tenant_id=7832,
        name="Panel 59",
        panel_type="PASARGUARD",
    )
    panel.integration = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")

    real_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("app.services.oc_panel_local_ops") or name.startswith(
            "app.services.oc_rebecca_integration"
        ):
            raise AssertionError(f"unexpected import of {name} on OC success path")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=guarded_import), patch(
        "app.services.oc_connection_credentials.connection_token_for_panel",
        new=AsyncMock(return_value="conn"),
    ), patch(
        "app.services.oc_panel_metadata_sync.ensure_panel_metadata_for_ops",
        new=AsyncMock(),
    ), patch(
        "app.services.oc_connection_credentials.call_oc_api",
        new=AsyncMock(return_value={"external_user_id": "pasarguard_discovery_1805", "status": "active"}),
    ):
        data = await call_oc_panel_api(None, panel, "POST", "/v1/integration/panels/1805/test-user")

    assert data["external_user_id"] == "pasarguard_discovery_1805"


@pytest.mark.asyncio
async def test_call_oc_panel_api_pasarguard_does_not_require_httpx(monkeypatch: pytest.MonkeyPatch):
    """Regression: eager Rebecca imports broke PASARGUARD test-user when httpx was absent."""
    from app.db.models_oc import OCPanel, OCIntegration
    from app.services.oc_connection_credentials import call_oc_panel_api

    monkeypatch.setitem(sys.modules, "httpx", None)

    panel = OCPanel(
        integration_id=1,
        source_panel_id="1805",
        purchaser_identity="1314",
        oc_account_id=1314,
        tenant_id=7832,
        name="Panel 59",
        panel_type="PASARGUARD",
    )
    panel.integration = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")

    with patch(
        "app.services.oc_connection_credentials.connection_token_for_panel",
        new=AsyncMock(return_value="conn"),
    ), patch(
        "app.services.oc_panel_metadata_sync.ensure_panel_metadata_for_ops",
        new=AsyncMock(),
    ), patch(
        "app.services.oc_connection_credentials.call_oc_api",
        new=AsyncMock(return_value={"external_user_id": "pasarguard_discovery_1805"}),
    ):
        data = await call_oc_panel_api(None, panel, "POST", "/v1/integration/panels/1805/test-user")

    assert data["external_user_id"] == "pasarguard_discovery_1805"
