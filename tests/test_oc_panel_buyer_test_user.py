"""BUG 2: additive buyer test-user path vs preserved remote test-user."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_call_oc_panel_api_does_not_use_maybe_local_before_remote():
    """Standard panel API calls must still reach remote OC first (PANEL_TYPE_UNSUPPORTED fallback only)."""
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

    with patch(
        "app.services.oc_connection_credentials.connection_token_for_panel",
        new=AsyncMock(return_value="conn"),
    ), patch(
        "app.services.oc_panel_metadata_sync.ensure_panel_metadata_for_ops",
        new=AsyncMock(),
    ), patch(
        "app.services.oc_panel_local_ops.maybe_local_panel_api",
        new=AsyncMock(return_value={"external_user_id": "local-only"}),
    ) as local_mock, patch(
        "app.services.oc_connection_credentials.call_oc_api",
        new=AsyncMock(return_value={"external_user_id": "remote-oc"}),
    ) as remote_mock:
        data = await call_oc_panel_api(
            None, panel, "POST", "/v1/integration/panels/135/test-user"
        )

    assert data["external_user_id"] == "remote-oc"
    remote_mock.assert_awaited_once()
    local_mock.assert_not_called()


@pytest.mark.asyncio
async def test_provision_test_user_via_buyer_panel_success():
    from app.db.models_oc import OCPanel
    from app.services.oc_panel_buyer_test_user import provision_test_user_via_buyer_panel

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="804",
        name="R",
        panel_type="REBECCA",
    )
    with patch(
        "app.services.oc_panel_buyer_test_user.connection_token_for_panel",
        new=AsyncMock(return_value="tok"),
    ), patch(
        "app.services.oc_panel_buyer_test_user.resolve_buyer_auth",
        new=AsyncMock(return_value=MagicMock()),
    ), patch(
        "app.services.oc_panel_buyer_test_user.execute_local_panel_api",
        new=AsyncMock(return_value={"external_user_id": "pasarguard_discovery_135", "status": "active"}),
    ) as local_exec:
        data = await provision_test_user_via_buyer_panel(AsyncMock(), panel)

    assert data["external_user_id"] == "pasarguard_discovery_135"
    local_exec.assert_awaited_once()


@pytest.mark.asyncio
async def test_provision_test_user_via_buyer_panel_rejects_non_rebecca():
    from app.db.models_oc import OCPanel
    from app.services.oc_panel_buyer_test_user import provision_test_user_via_buyer_panel

    panel = OCPanel(
        integration_id=1,
        source_panel_id="1",
        purchaser_identity="1",
        name="P",
        panel_type="PASARGUARD",
    )
    with pytest.raises(HTTPException) as exc:
        await provision_test_user_via_buyer_panel(AsyncMock(), panel)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_provision_test_user_via_buyer_panel_requires_credentials():
    from app.db.models_oc import OCPanel
    from app.services.oc_panel_buyer_test_user import provision_test_user_via_buyer_panel

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="804",
        name="R",
        panel_type="REBECCA",
    )
    with patch(
        "app.services.oc_panel_buyer_test_user.connection_token_for_panel",
        new=AsyncMock(return_value="tok"),
    ), patch(
        "app.services.oc_panel_buyer_test_user.resolve_buyer_auth",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.oc_panel_buyer_test_user.try_refresh_buyer_auth_from_oc",
        new=AsyncMock(return_value=False),
    ):
        with pytest.raises(HTTPException) as exc:
            await provision_test_user_via_buyer_panel(AsyncMock(), panel)
    assert exc.value.status_code == 409
