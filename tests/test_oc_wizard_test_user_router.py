"""Router-level regression for wizard test-user (HTTP mapping and persistence)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.oc_integration_client import OcIntegrationApiError, http_exception_from_oc_error


@pytest.mark.asyncio
async def test_create_test_user_persists_rotated_external_user_id():
    from app.db.models_oc import OCPanel, OCIntegration
    from app.routers.integration import create_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="20",
        tenant_id=1,
        name="R",
        panel_type="REBECCA",
    )
    panel.integration = OCIntegration(
        base_url="https://oc.example",
        api_token_encrypted="enc",
        token_preview="t",
    )
    rotated = "c3d4e5f6-a7b8-9012-cdef-123456789012"
    admin = MagicMock()
    ctx = ("admin", False, admin)

    db = AsyncMock()
    db.commit = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = panel
    db.execute = AsyncMock(return_value=execute_result)

    with patch(
        "app.routers.integration.ensure_actor_panel_access",
        new=AsyncMock(),
    ), patch(
        "app.routers.integration.require_active_telegram_for_tenant_admin",
        new=AsyncMock(),
    ), patch(
        "app.routers.integration._require_tenant_admin",
        return_value=admin,
    ), patch(
        "app.services.oc_wizard_test_user.provision_wizard_test_user",
        new=AsyncMock(return_value={"external_user_id": rotated}),
    ):
        resp = await create_test_user(panel_id=6456, db=db, user_context=ctx)

    assert resp.test_user_id == rotated
    assert panel.test_user_id == rotated
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_create_test_user_maps_identity_blocked_to_existing_http_contract():
    from app.db.models_oc import OCPanel, OCIntegration
    from app.routers.integration import create_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="20",
        tenant_id=1,
        name="R",
        panel_type="REBECCA",
    )
    panel.integration = OCIntegration(
        base_url="https://oc.example",
        api_token_encrypted="enc",
        token_preview="t",
    )
    admin = MagicMock()
    ctx = ("admin", False, admin)
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = panel
    db.execute = AsyncMock(return_value=execute_result)

    err = OcIntegrationApiError(oc_status=409, detail="USER_IDENTITY_BLOCKED")
    with patch(
        "app.routers.integration.ensure_actor_panel_access",
        new=AsyncMock(),
    ), patch(
        "app.routers.integration.require_active_telegram_for_tenant_admin",
        new=AsyncMock(),
    ), patch(
        "app.routers.integration._require_tenant_admin",
        return_value=admin,
    ), patch(
        "app.services.oc_wizard_test_user.provision_wizard_test_user",
        new=AsyncMock(side_effect=err),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await create_test_user(panel_id=6456, db=db, user_context=ctx)

    expected = http_exception_from_oc_error(err)
    assert exc_info.value.status_code == expected.status_code
    assert exc_info.value.detail == expected.detail


@pytest.mark.asyncio
async def test_create_test_user_reuses_stored_id_without_calling_helper_twice():
    from app.db.models_oc import OCPanel, OCIntegration
    from app.routers.integration import create_test_user

    stored = "opaque-discovery-id-xyz"
    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="20",
        tenant_id=1,
        name="R",
        panel_type="REBECCA",
        test_user_id=stored,
    )
    panel.integration = OCIntegration(
        base_url="https://oc.example",
        api_token_encrypted="enc",
        token_preview="t",
    )
    admin = MagicMock()
    ctx = ("admin", False, admin)
    db = AsyncMock()
    db.commit = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = panel
    db.execute = AsyncMock(return_value=execute_result)

    provision = AsyncMock(return_value={"external_user_id": stored})
    with patch(
        "app.routers.integration.ensure_actor_panel_access",
        new=AsyncMock(),
    ), patch(
        "app.routers.integration.require_active_telegram_for_tenant_admin",
        new=AsyncMock(),
    ), patch(
        "app.routers.integration._require_tenant_admin",
        return_value=admin,
    ), patch(
        "app.services.oc_wizard_test_user.provision_wizard_test_user",
        provision,
    ):
        resp = await create_test_user(panel_id=6456, db=db, user_context=ctx)

    assert resp.test_user_id == stored
    provision.assert_awaited_once()


@pytest.mark.asyncio
async def test_host_subscription_path_accepts_opaque_test_user_id():
    from app.db.models_oc import OCPanel, OCIntegration
    from app.services.oc_panel_host_subscription import refresh_panel_hosts_from_discovery_subscription

    opaque = "d4e5f6a7-b8c9-0123-def0-234567890abc"
    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="20",
        name="R",
        test_user_id=opaque,
    )
    panel.integration = OCIntegration(
        base_url="https://oc.example",
        api_token_encrypted="enc",
        token_preview="t",
    )

    with patch(
        "app.services.oc_panel_host_subscription._selected_group_ids",
        new=AsyncMock(return_value=[]),
    ):
        count = await refresh_panel_hosts_from_discovery_subscription(AsyncMock(), panel)

    assert count == 0
