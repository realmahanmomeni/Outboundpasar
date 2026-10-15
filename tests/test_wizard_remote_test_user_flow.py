"""BUG 2 / OPT 1: wizard uses remote OC test-user (same path as PASARGUARD panels)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.oc_integration_client import OcIntegrationApiError


def test_wizard_calls_remote_test_user_not_buyer_endpoint():
    source = Path("dashboard_src/src/features/panels/dialogs/add-panel-wizard-modal.tsx").read_text(
        encoding="utf-8"
    )
    assert "createTestUser(" in source
    assert "createTestUserViaBuyer" not in source
    assert "/test-user/buyer" not in source


def test_panels_api_still_exposes_buyer_helper_for_non_wizard_callers():
    api = Path("dashboard_src/src/features/panels/service/panels-api.ts").read_text(encoding="utf-8")
    assert "createTestUserViaBuyer" in api
    assert "`/api/integration/panels/${panelId}/test-user`" in api or "/test-user`" in api


@pytest.mark.asyncio
async def test_create_test_user_rebecca_uses_provision_wizard_test_user():
    """Router delegates to Mahan's provision_wizard_test_user (remote OC via call_oc_panel_api)."""
    from app.db.models_oc import OCIntegration, OCPanel
    from app.routers.integration import create_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="1",
        oc_account_id=1,
        tenant_id=1,
        name="Rebecca panel",
        panel_type="REBECCA",
    )
    panel.id = 6456
    panel.integration = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
    panel.test_user_id = None

    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=panel))
    )
    db.commit = AsyncMock()

    with patch(
        "app.routers.integration.ensure_actor_panel_access",
        new=AsyncMock(),
    ), patch(
        "app.routers.integration.require_active_telegram_for_tenant_admin",
        new=AsyncMock(),
    ), patch(
        "app.routers.integration._require_tenant_admin",
        return_value=MagicMock(),
    ), patch(
        "app.services.oc_wizard_test_user.call_oc_panel_api",
        new=AsyncMock(return_value={"external_user_id": "pasarguard_discovery_135"}),
    ) as oc_call:
        resp = await create_test_user(
            panel_id=6456,
            db=db,
            user_context=("admin", False, MagicMock()),
        )

    oc_call.assert_awaited_once()
    args = oc_call.await_args
    assert args[0][2] == "POST"
    assert args[0][3].endswith("/test-user")
    assert resp.test_user_id == "pasarguard_discovery_135"


@pytest.mark.asyncio
async def test_rebecca_groups_configs_use_same_call_oc_panel_api_as_pasarguard():
    from app.db.models_oc import OCIntegration, OCPanel
    from app.services.oc_connection_credentials import call_oc_panel_api

    for panel_type in ("PASARGUARD", "REBECCA"):
        panel = OCPanel(
            integration_id=1,
            source_panel_id="40",
            purchaser_identity="1",
            oc_account_id=1,
            tenant_id=1,
            name="Panel",
            panel_type=panel_type,
        )
        panel.integration = OCIntegration(
            base_url="https://oc.example", api_token_encrypted="e", token_preview="t"
        )
        with patch(
            "app.services.oc_connection_credentials.connection_token_for_panel",
            new=AsyncMock(return_value="conn"),
        ), patch(
            "app.services.oc_panel_metadata_sync.ensure_panel_metadata_for_ops",
            new=AsyncMock(),
        ), patch(
            "app.services.oc_connection_credentials.call_oc_api",
            new=AsyncMock(return_value={"groups": [{"id": "1", "name": "g"}]}),
        ) as remote:
            data = await call_oc_panel_api(
                None, panel, "GET", "/v1/integration/panels/40/groups"
            )
        assert data["groups"][0]["id"] == "1"
        remote.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_oc_panel_api_does_not_invoke_local_rebecca_ops():
    from app.db.models_oc import OCIntegration, OCPanel
    from app.services.oc_connection_credentials import call_oc_panel_api

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="1",
        oc_account_id=1,
        tenant_id=1,
        name="R",
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
    ), patch(
        "app.services.oc_panel_local_ops.execute_local_panel_api",
        new=AsyncMock(),
    ) as local_mock:
        with pytest.raises(OcIntegrationApiError):
            await call_oc_panel_api(None, panel, "POST", "/v1/integration/panels/135/test-user")
    local_mock.assert_not_called()
