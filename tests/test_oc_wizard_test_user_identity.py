"""Wizard test-user: USER_IDENTITY_BLOCKED recovery via one bounded PUT rotation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.oc_integration_client import OcIntegrationApiError


@pytest.mark.asyncio
async def test_wizard_test_user_success_without_rotation():
    from app.db.models_oc import OCPanel
    from app.services.oc_wizard_test_user import provision_wizard_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="166",
        purchaser_identity="20",
        name="PG",
        panel_type="PASARGUARD",
    )
    with patch(
        "app.services.oc_wizard_test_user.call_oc_panel_api",
        new=AsyncMock(return_value={"external_user_id": "pasarguard_discovery_166", "status": "active"}),
    ) as api:
        data = await provision_wizard_test_user(AsyncMock(), panel)

    assert data["external_user_id"] == "pasarguard_discovery_166"
    api.assert_awaited_once()
    assert api.await_args.args[2] == "POST"
    assert api.await_args.args[3].endswith("/test-user")


@pytest.mark.asyncio
async def test_wizard_test_user_rotates_once_on_identity_blocked():
    from app.db.models_oc import OCPanel
    from app.services.oc_wizard_test_user import provision_wizard_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="20",
        name="Rebecca",
        panel_type="REBECCA",
    )
    rotated = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    async def _side_effect(db, p, method, path, json=None, **kwargs):
        if method == "POST":
            raise OcIntegrationApiError(oc_status=409, detail="USER_IDENTITY_BLOCKED")
        if method == "PUT":
            assert json == {"groups": [], "configs": []}
            assert f"/users/{rotated}" in path or "/users/" in path
            return {"external_user_id": rotated, "status": "active", "groups": [], "configs": []}
        raise AssertionError(f"unexpected {method} {path}")

    with patch(
        "app.services.oc_wizard_test_user.call_oc_panel_api",
        new=AsyncMock(side_effect=_side_effect),
    ) as api, patch(
        "app.services.oc_wizard_test_user.uuid.uuid4",
        return_value=__import__("uuid").UUID(rotated),
    ):
        data = await provision_wizard_test_user(AsyncMock(), panel)

    assert data["external_user_id"] == rotated
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_wizard_test_user_rotation_terminal_failure():
    from app.db.models_oc import OCPanel
    from app.services.oc_wizard_test_user import provision_wizard_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="1711",
        purchaser_identity="1012",
        name="Rebecca",
        panel_type="REBECCA",
    )

    async def _side_effect(db, p, method, path, json=None, **kwargs):
        if method == "POST":
            raise OcIntegrationApiError(oc_status=409, detail="USER_IDENTITY_BLOCKED")
        raise OcIntegrationApiError(oc_status=409, detail="USER_IDENTITY_BLOCKED")

    with patch(
        "app.services.oc_wizard_test_user.call_oc_panel_api",
        new=AsyncMock(side_effect=_side_effect),
    ):
        with pytest.raises(OcIntegrationApiError) as exc:
            await provision_wizard_test_user(AsyncMock(), panel)
    assert exc.value.code == "USER_IDENTITY_BLOCKED"


@pytest.mark.asyncio
async def test_wizard_test_user_does_not_retry_other_409_codes():
    from app.db.models_oc import OCPanel
    from app.services.oc_wizard_test_user import provision_wizard_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="20",
        name="Rebecca",
        panel_type="REBECCA",
    )
    with patch(
        "app.services.oc_wizard_test_user.call_oc_panel_api",
        new=AsyncMock(
            side_effect=OcIntegrationApiError(oc_status=409, detail="USER_EXISTS"),
        ),
    ) as api:
        with pytest.raises(OcIntegrationApiError) as exc:
            await provision_wizard_test_user(AsyncMock(), panel)
    assert exc.value.code == "USER_EXISTS"
    api.assert_awaited_once()


@pytest.mark.asyncio
async def test_wizard_test_user_reuses_persisted_id_without_api_calls():
    from app.db.models_oc import OCPanel
    from app.services.oc_wizard_test_user import provision_wizard_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="20",
        name="Rebecca",
        panel_type="REBECCA",
        test_user_id="rotated-uuid-already-stored",
    )
    with patch(
        "app.services.oc_wizard_test_user.call_oc_panel_api",
        new=AsyncMock(),
    ) as api:
        data = await provision_wizard_test_user(AsyncMock(), panel)

    assert data["external_user_id"] == "rotated-uuid-already-stored"
    api.assert_not_called()


@pytest.mark.asyncio
async def test_wizard_test_user_identity_blocked_parsed_from_detail_token():
    from app.db.models_oc import OCPanel
    from app.services.oc_wizard_test_user import provision_wizard_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="20",
        name="Rebecca",
        panel_type="REBECCA",
    )
    rotated = "b2c3d4e5-f6a7-8901-bcde-f12345678901"

    async def _side_effect(db, p, method, path, json=None, **kwargs):
        if method == "POST":
            raise OcIntegrationApiError(oc_status=409, detail="prefix USER_IDENTITY_BLOCKED suffix")
        return {"external_user_id": rotated, "status": "active"}

    with patch(
        "app.services.oc_wizard_test_user.call_oc_panel_api",
        new=AsyncMock(side_effect=_side_effect),
    ) as api, patch(
        "app.services.oc_wizard_test_user.uuid.uuid4",
        return_value=__import__("uuid").UUID(rotated),
    ):
        data = await provision_wizard_test_user(AsyncMock(), panel)

    assert data["external_user_id"] == rotated
    assert api.await_count == 2


@pytest.mark.asyncio
async def test_wizard_test_user_put_missing_external_user_id_fails():
    from app.db.models_oc import OCPanel
    from app.services.oc_wizard_test_user import provision_wizard_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="20",
        name="Rebecca",
        panel_type="REBECCA",
    )

    async def _side_effect(db, p, method, path, json=None, **kwargs):
        if method == "POST":
            raise OcIntegrationApiError(oc_status=409, detail="USER_IDENTITY_BLOCKED")
        return {"status": "active", "groups": []}

    with patch(
        "app.services.oc_wizard_test_user.call_oc_panel_api",
        new=AsyncMock(side_effect=_side_effect),
    ):
        with pytest.raises(OcIntegrationApiError) as exc:
            await provision_wizard_test_user(AsyncMock(), panel)
    assert exc.value.oc_status == 502
    assert "missing external_user_id" in (exc.value.detail or "")


@pytest.mark.asyncio
async def test_wizard_test_user_post_missing_external_user_id_fails():
    from app.db.models_oc import OCPanel
    from app.services.oc_wizard_test_user import provision_wizard_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="166",
        purchaser_identity="20",
        name="PG",
        panel_type="PASARGUARD",
    )
    with patch(
        "app.services.oc_wizard_test_user.call_oc_panel_api",
        new=AsyncMock(return_value={"status": "active"}),
    ):
        with pytest.raises(OcIntegrationApiError) as exc:
            await provision_wizard_test_user(AsyncMock(), panel)
    assert exc.value.oc_status == 502


@pytest.mark.asyncio
async def test_wizard_test_user_concurrent_duplicate_provision_both_call_oc():
    """Without row locking, concurrent empty test_user_id requests may each hit OC (documented risk)."""
    import asyncio

    from app.db.models_oc import OCPanel
    from app.services.oc_wizard_test_user import provision_wizard_test_user

    panel = OCPanel(
        integration_id=1,
        source_panel_id="135",
        purchaser_identity="20",
        name="Rebecca",
        panel_type="REBECCA",
    )
    call_count = 0

    async def _api(db, p, method, path, json=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if method == "POST":
            raise OcIntegrationApiError(oc_status=409, detail="USER_IDENTITY_BLOCKED")
        return {
            "external_user_id": f"uuid-{call_count}",
            "status": "active",
        }

    with patch(
        "app.services.oc_wizard_test_user.call_oc_panel_api",
        new=AsyncMock(side_effect=_api),
    ):
        results = await asyncio.gather(
            provision_wizard_test_user(AsyncMock(), panel),
            provision_wizard_test_user(AsyncMock(), panel),
        )

    assert len({r["external_user_id"] for r in results}) == 2
    assert call_count == 4
