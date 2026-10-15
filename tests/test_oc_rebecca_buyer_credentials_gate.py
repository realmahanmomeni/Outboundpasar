"""Rebecca panel calls use Outbound Center HTTP (no local buyer-credential fallback)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.services.oc_integration_client import OcIntegrationApiError


@pytest.mark.asyncio
async def test_rebecca_panel_type_unsupported_propagates_from_oc():
    from app.db.models_oc import OCPanel, OCIntegration
    from app.services.oc_connection_credentials import call_oc_panel_api

    panel = OCPanel(
        integration_id=1,
        source_panel_id="342",
        purchaser_identity="390",
        oc_account_id=390,
        tenant_id=7416,
        name="Rebecca",
        panel_type="REBECCA",
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
        new=AsyncMock(
            side_effect=OcIntegrationApiError(oc_status=501, detail="PANEL_TYPE_UNSUPPORTED"),
        ),
    ):
        with pytest.raises(OcIntegrationApiError) as exc:
            await call_oc_panel_api(None, panel, "POST", "/v1/integration/panels/342/test-user")
    assert exc.value.code == "PANEL_TYPE_UNSUPPORTED"
