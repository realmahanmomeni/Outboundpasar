"""Discovery user PUT must not drop all configs when only some are rejected."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from app.db import GetDB
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCPanelDestinationHost, OCPanelGroup
from app.services.oc_integration_client import OcIntegrationApiError
from app.services.oc_panel_host_subscription import (
    _put_discovery_user,
    refresh_panel_hosts_from_discovery_subscription,
    rejected_config_ids_from_oc_detail,
)
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import subscription_config_links, subscription_link_uri_fingerprint


def test_subscription_config_links_dedupes_identical_uri_without_fragment():
    """Different catalog/remark fragments with the same URI base collapse to one destination."""
    base = "vless://same-uuid@1.1.1.1:443?encryption=none"
    links = [f"{base}#Config%20A", f"{base}#Config%20B"]
    parsed = subscription_config_links(links)
    assert len(parsed) == 1
    assert subscription_link_uri_fingerprint(links[0]) == subscription_link_uri_fingerprint(links[1])


async def test_reconcile_collapses_duplicate_uri_links_to_one_destination_host():
    base = "vless://dup@2.2.2.2:443?encryption=none"
    links = [f"{base}#One", f"{base}#Two"]
    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="x", purchaser_identity="b", name="P")
        db.add(panel)
        await db.flush()
        count = await reconcile_panel_destination_hosts(db, panel, links)
        assert count == 1
        await db.rollback()


async def test_put_discovery_user_groups_only_after_all_configs_rejected():
    put_calls: list[dict] = []

    async def _mock_put(db, panel, method, path, json=None):
        put_calls.append(json or {})
        configs = list((json or {}).get("configs") or [])
        if configs:
            raise OcIntegrationApiError(
                oc_status=400,
                detail=f"CONFIG_NOT_IN_GROUPS: {configs!r}",
            )
        return {"subscription_url": "https://oc.example/sub"}

    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="x", purchaser_identity="b", name="P", test_user_id="t")
        db.add(panel)
        await db.flush()
        with patch(
            "app.services.oc_panel_host_subscription.call_oc_panel_api",
            new_callable=AsyncMock,
            side_effect=_mock_put,
        ):
            data = await _put_discovery_user(db, panel, "/users/t", ["g1"], ["bad-a", "bad-b"])
        assert data["subscription_url"]
        assert len(put_calls) == 2
        assert put_calls[0]["configs"] == ["bad-a", "bad-b"]
        assert put_calls[1]["configs"] == []


def test_rejected_config_ids_from_oc_detail_parses_list():
    detail = "CONFIG_NOT_IN_GROUPS: ['inbound-a', 'inbound-b']"
    assert rejected_config_ids_from_oc_detail(detail) == {"inbound-a", "inbound-b"}


async def test_discovery_put_keeps_valid_configs_when_some_not_in_group():
    five_configs = [f"cfg-{i}" for i in range(5)]
    sub_links = [
        f"vless://u{i}@{i}.{i}.{i}.{i}:443?encryption=none#Host%20{i}"
        for i in range(1, 6)
    ]
    put_calls: list[dict] = []

    async def _mock_put(db, panel, method, path, json=None):
        assert method == "PUT"
        body = json or {}
        put_calls.append(body)
        configs = list(body.get("configs") or [])
        if len(configs) == 5:
            raise OcIntegrationApiError(
                oc_status=400,
                detail="CONFIG_NOT_IN_GROUPS: ['cfg-3', 'cfg-4']",
            )
        if configs == ["cfg-0", "cfg-1", "cfg-2"]:
            return {"subscription_url": "https://oc.example/sub/token"}
        if not configs and len(put_calls) == 1:
            raise OcIntegrationApiError(oc_status=400, detail="unexpected")
        return {"subscription_url": "https://oc.example/sub/token"}

    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="1711",
            purchaser_identity="buyer",
            name="Panel 45",
            test_user_id="disc-user",
        )
        db.add(panel)
        await db.flush()
        db.add(
            OCPanelGroup(
                panel_id=panel.id,
                source_group_id="g1",
                source_name="Group",
                is_selected=True,
            )
        )
        for cid in five_configs:
            db.add(
                OCPanelConfig(
                    panel_id=panel.id,
                    source_config_id=cid,
                    source_name=cid,
                    source_payload={
                        "group_mapping": {"supported": True, "groups": ["g1"]},
                    },
                )
            )
        await db.flush()

        with (
            patch(
                "app.services.oc_panel_host_subscription.call_oc_panel_api",
                new_callable=AsyncMock,
                side_effect=_mock_put,
            ),
            patch(
                "app.services.oc_panel_host_subscription.fetch_upstream_subscription_body",
                new_callable=AsyncMock,
                return_value="\n".join(sub_links),
            ),
        ):
            updated = await refresh_panel_hosts_from_discovery_subscription(db, panel)

        assert updated == 5
        assert put_calls[0]["configs"] == five_configs
        assert put_calls[1]["configs"] == ["cfg-0", "cfg-1", "cfg-2"]
        assert "cfg-3" not in put_calls[1]["configs"]
        assert "cfg-4" not in put_calls[1]["configs"]
        dest_count = await db.scalar(
            select(func.count())
            .select_from(OCPanelDestinationHost)
            .where(
                OCPanelDestinationHost.panel_id == panel.id,
                OCPanelDestinationHost.source_missing.is_(False),
            )
        )
        assert dest_count == 5
        await db.rollback()
