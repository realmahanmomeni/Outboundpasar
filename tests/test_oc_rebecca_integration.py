"""Rebecca local integration adapter (mocked panel HTTP)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.oc_rebecca_integration import (
    DISCOVERY_EXTERNAL_USER_ID,
    get_panel_configs,
    get_panel_groups,
    panel_username_for,
    provision_external_user,
    provision_test_user,
)
from app.services.oc_panel_buyer_auth import BuyerPanelAuth
from app.services.panel_drivers.exceptions import ClientExistsError, ClientNotFoundError
from app.services.panel_drivers.types import VpnGroupInfo, VpnInboundInfo, VpnUserInfo


@pytest.fixture
def auth() -> BuyerPanelAuth:
    return BuyerPanelAuth(
        panel_url="https://rebecca.example",
        username="buyer",
        password="secret",
    )


@pytest.mark.asyncio
async def test_rebecca_local_groups_and_configs(auth):
    groups = [
        VpnGroupInfo("10", "Premium", ("vless-tcp",)),
        VpnGroupInfo("11", "Basic", ("vless-tcp", "trojan-ws")),
    ]
    inbounds = [
        VpnInboundInfo("vless-tcp", protocol="vless", network="tcp"),
        VpnInboundInfo("trojan-ws", protocol="trojan", network="ws"),
    ]
    with patch(
        "app.services.oc_rebecca_integration._DRIVER.list_vpn_groups",
        new=AsyncMock(return_value=groups),
    ), patch(
        "app.services.oc_rebecca_integration._DRIVER.list_vpn_inbounds",
        new=AsyncMock(return_value=inbounds),
    ):
        raw_groups = await get_panel_groups(auth)
        assert {g["id"] for g in raw_groups} == {"10", "11"}
        configs = await get_panel_configs(auth)
        ids = {c["id"] for c in configs}
        assert ids == {"vless-tcp", "trojan-ws"}
        premium = next(c for c in configs if c["id"] == "vless-tcp")
        assert set(premium["group_mapping"]["groups"]) == {"10", "11"}


@pytest.mark.asyncio
async def test_rebecca_provision_test_user(auth):
    user_info = VpnUserInfo(
        username="pg14_abc",
        status="active",
        group_ids=("10",),
        subscription_url="https://rebecca.example/sub/abc",
    )
    with patch(
        "app.services.oc_rebecca_integration._DRIVER.create_vpn_user",
        new=AsyncMock(return_value=user_info),
    ), patch(
        "app.services.oc_rebecca_integration._DRIVER.list_vpn_groups",
        new=AsyncMock(return_value=[VpnGroupInfo("10", "Premium", ("vless-tcp",))]),
    ):
        ext = await provision_test_user(auth, account_id=804, subscription_id=14)
        assert ext == "pasarguard_discovery_14"
        state = await provision_external_user(
            auth,
            account_id=804,
            subscription_id=14,
            external_user_id=DISCOVERY_EXTERNAL_USER_ID,
            groups=[],
            configs=[],
        )
        assert state["subscription_url"] == "https://rebecca.example/sub/abc"
        assert "vless-tcp" in state["configs"]


@pytest.mark.asyncio
async def test_rebecca_provision_phantom_exists_recreates(auth):
    vpn_username = panel_username_for(804, 14, "ext-1")
    user_info = VpnUserInfo(
        username=vpn_username,
        status="active",
        group_ids=("10",),
        subscription_url="https://rebecca.example/sub/abc",
    )
    create = AsyncMock(side_effect=[ClientExistsError("exists", username=vpn_username), user_info])
    update = AsyncMock(side_effect=ClientNotFoundError("missing"))
    delete = AsyncMock(return_value=False)
    with (
        patch("app.services.oc_rebecca_integration._DRIVER.create_vpn_user", new=create),
        patch("app.services.oc_rebecca_integration._DRIVER.update_vpn_user", new=update),
        patch("app.services.oc_rebecca_integration._DRIVER.delete_vpn_user", new=delete),
        patch(
            "app.services.oc_rebecca_integration._DRIVER.list_vpn_groups",
            new=AsyncMock(return_value=[VpnGroupInfo("10", "Premium", ("vless-tcp",))]),
        ),
    ):
        state = await provision_external_user(
            auth,
            account_id=804,
            subscription_id=14,
            external_user_id="ext-1",
            groups=["10"],
            configs=["vless-tcp"],
        )
    assert state["subscription_url"] == "https://rebecca.example/sub/abc"
    update.assert_awaited_once()
    delete.assert_awaited_once()
    assert create.await_count == 2
