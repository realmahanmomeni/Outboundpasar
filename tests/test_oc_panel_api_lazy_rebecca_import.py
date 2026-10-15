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

    with patch(
        "app.services.oc_connection_credentials.connection_token_for_panel",
        new=AsyncMock(return_value="conn"),
    ), patch(
        "app.services.oc_panel_metadata_sync.ensure_panel_metadata_for_ops",
        new=AsyncMock(),
    ), patch(
        "app.services.oc_panel_local_ops.maybe_local_panel_api",
        new=AsyncMock(return_value=None),
    ) as local_mock, patch(
        "app.services.oc_connection_credentials.call_oc_api",
        new=AsyncMock(return_value={"external_user_id": "pasarguard_discovery_1805", "status": "active"}),
    ) as remote_mock:
        data = await call_oc_panel_api(None, panel, "POST", "/v1/integration/panels/1805/test-user")

    assert data["external_user_id"] == "pasarguard_discovery_1805"
    local_mock.assert_awaited_once()
    remote_mock.assert_awaited_once()


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
        "app.services.oc_panel_local_ops.maybe_local_panel_api",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.oc_connection_credentials.call_oc_api",
        new=AsyncMock(return_value={"external_user_id": "pasarguard_discovery_1805"}),
    ):
        data = await call_oc_panel_api(None, panel, "POST", "/v1/integration/panels/1805/test-user")

    assert data["external_user_id"] == "pasarguard_discovery_1805"
