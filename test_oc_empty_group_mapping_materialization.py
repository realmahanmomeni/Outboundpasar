"""Regression: OC configs with supported=True and empty groups[] import catalog metadata."""
import asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select

from app.db import GetDB
from app.db.models import ProxyHost
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelDestinationHost, OCPanelGroup
from app.routers.integration import sync_configs_and_hosts, SyncRequest
from app.routers.panel import list_panel_hosts
from app.services.oc_panel_materialization import oc_config_import_eligible
from app.utils.jwt import create_admin_token
from app.routers.panel import get_current_user_context
from test_phase5_api import MockRequest

import app.routers.integration as integration_router


async def mock_oc_panel_call(db, panel, method, path, json=None):
    if "groups" in path:
        return {"groups": [{"id": "1", "name": "G1"}]}
    if "configs" in path:
        return {
            "configs": [
                {
                    "id": "vless-tcp",
                    "name": "VLESS TCP",
                    "group_mapping": {"supported": True, "groups": []},
                }
            ]
        }
    if method == "PUT" and "/users/" in path:
        return {"subscription_url": "https://oc.example/sub/t"}
    return {}


async def run_tests():
    integration_router._oc_panel_call = mock_oc_panel_call
    assert oc_config_import_eligible({"supported": True, "groups": []}, ["1"]) is True
    assert oc_config_import_eligible({"supported": True, "groups": ["2"]}, ["1"]) is False
    assert oc_config_import_eligible({"supported": False, "groups": []}, []) is True

    link = "vless://u@1.1.1.1:443?encryption=none#VLESS%20TCP"

    async with GetDB() as db:
        panel = OCPanel(
            integration_id=1,
            source_panel_id="empty-map-test",
            purchaser_identity="admin",
            name="Empty mapping panel",
            sync_status="pending",
            test_user_id="discovery",
        )
        db.add(panel)
        await db.flush()
        panel_id = panel.id

        owner_ctx = await get_current_user_context(
            MockRequest(), db, token=await create_admin_token(1, "admin")
        )
        sync_req = SyncRequest(selected_group_ids=["1"], group_names={"1": "G1"})
        async def _mock_put(db, panel, method, path, json=None):
            if method == "PUT" and "/users/" in path:
                return {"subscription_url": "https://oc.example/sub/t"}
            return await mock_oc_panel_call(db, panel, method, path, json)

        with (
            patch(
                "app.services.oc_panel_host_subscription.call_oc_panel_api",
                new_callable=AsyncMock,
                side_effect=_mock_put,
            ),
            patch(
                "app.services.oc_panel_host_subscription.fetch_upstream_subscription_body",
                new_callable=AsyncMock,
                return_value=link,
            ),
        ):
            resp = await sync_configs_and_hosts(panel_id, sync_req, db, owner_ctx)
        assert resp.configs_created == 1
        assert resp.hosts_created == 1

        hosts = await list_panel_hosts(panel_id, db, owner_ctx)
        assert len(hosts) == 1
        assert hosts[0].display_name == "VLESS TCP"

        cfg = (
            await db.execute(select(OCPanelConfig).where(OCPanelConfig.panel_id == panel_id))
        ).scalar_one()
        assert cfg.locally_hidden is True
        assert cfg.virtual_inbound_tag is None
        legacy = (
            await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == f"oc_{panel_id}_vless-tcp"))
        ).scalar_one_or_none()
        assert legacy is None

        await db.execute(delete(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel_id))
        await db.execute(delete(OCPanelConfig).where(OCPanelConfig.panel_id == panel_id))
        await db.execute(delete(OCPanelGroup).where(OCPanelGroup.panel_id == panel_id))
        await db.execute(delete(OCPanel).where(OCPanel.id == panel_id))
        await db.commit()
    print("test_oc_empty_group_mapping_materialization: OK")


if __name__ == "__main__":
    asyncio.run(run_tests())
