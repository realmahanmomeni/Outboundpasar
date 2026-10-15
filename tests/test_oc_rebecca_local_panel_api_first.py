"""BUG 2: Rebecca panels with buyer credentials use local test-user before remote OC."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_call_oc_panel_api_uses_local_rebecca_before_remote():
    from app.db.models_oc import OCPanel, OCIntegration
    from app.services.oc_connection_credentials import call_oc_panel_api
    from app.services.oc_panel_buyer_auth import BuyerPanelAuth

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="804",
        oc_account_id=804,
        tenant_id=1,
        name="Rebecca",
        panel_type="REBECCA",
        buyer_panel_url="https://panel.example",
        buyer_sub_admin_username="sub",
    )
    panel.integration = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
    auth = BuyerPanelAuth(panel_url="https://panel.example", username="sub", password="secret")

    with patch(
        "app.services.oc_connection_credentials.connection_token_for_panel",
        new=AsyncMock(return_value="conn"),
    ), patch(
        "app.services.oc_panel_metadata_sync.ensure_panel_metadata_for_ops",
        new=AsyncMock(),
    ), patch(
        "app.services.oc_panel_local_ops.maybe_local_panel_api",
        new=AsyncMock(return_value={"external_user_id": "pasarguard_discovery_135", "status": "active"}),
    ) as local_mock, patch(
        "app.services.oc_connection_credentials.call_oc_api",
        new=AsyncMock(),
    ) as remote_mock, patch(
        "app.services.oc_panel_local_ops.resolve_buyer_auth",
        new=AsyncMock(return_value=auth),
    ):
        data = await call_oc_panel_api(
            None, panel, "POST", "/v1/integration/panels/135/test-user"
        )

    assert data["external_user_id"] == "pasarguard_discovery_135"
    local_mock.assert_awaited_once()
    remote_mock.assert_not_called()
