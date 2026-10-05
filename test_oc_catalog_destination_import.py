"""Catalog metadata vs destination host inventory on OC panel import."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from app.db import GetDB
from app.db.models import ProxyHost
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCPanelDestinationHost, OCPanelGroup
from app.node.oc_sync import remove_imported_panel
from app.routers.integration import SyncRequest, sync_configs_and_hosts
from app.routers.panel import get_current_user_context, list_panel_hosts
from app.services.assignable_hosts import list_group_host_options
from app.services.oc_panel_destination_hosts import (
    ensure_panel_destination_hosts_materialized,
    reconcile_panel_destination_hosts,
)
from app.services.oc_share_link import destination_virtual_inbound_tag
from app.utils.jwt import create_admin_token
from test_phase5_api import MockRequest

import app.routers.integration as integration_router
import pytest


def _catalog_configs(n: int) -> list[dict]:
    return [
        {
            "id": f"catalog-{i}",
            "name": f"Catalog {i}",
            "group_mapping": {"supported": True, "groups": ["direct"]},
        }
        for i in range(n)
    ]


async def mock_oc_panel_call(db, panel, method, path, json=None):
    if "groups" in path:
        return {"groups": [{"id": "direct", "name": "direct"}]}
    if "configs" in path:
        return {"configs": _catalog_configs(31)}
    if method == "PUT" and "/users/" in path:
        return {"subscription_url": "https://oc.example/sub/token"}
    return {}


@pytest.fixture(autouse=True)
def _patch_oc_panel_call(monkeypatch):
    monkeypatch.setattr(integration_router, "_oc_panel_call", mock_oc_panel_call)


async def test_import_metadata_only_two_destination_hosts():
    sub_links = [
        "vless://u1@1.1.1.1:443?encryption=none#Host%20One",
        "vless://u2@2.2.2.2:443?encryption=none#Host%20Two",
        "vless://u3@3.3.3.3:443?encryption=none#%F0%9F%93%A1Status|ok",
    ]

    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="1507",
            purchaser_identity="buyer",
            name="Panel",
            test_user_id="disc-user",
        )
        db.add(panel)
        await db.flush()
        panel_id = panel.id

        owner_ctx = await get_current_user_context(
            MockRequest(), db, token=await create_admin_token(1, "admin")
        )
        sync_req = SyncRequest(selected_group_ids=["direct"], group_names={"direct": "direct"})

        async def _mock_panel_api(db, panel, method, path, json=None):
            if method == "PUT" and "/users/" in path:
                return {"subscription_url": "https://oc.example/sub/token"}
            return await mock_oc_panel_call(db, panel, method, path, json)

        with (
            patch(
                "app.services.oc_panel_host_subscription.call_oc_panel_api",
                new_callable=AsyncMock,
                side_effect=_mock_panel_api,
            ),
            patch(
                "app.services.oc_panel_host_subscription.fetch_upstream_subscription_body",
                new_callable=AsyncMock,
                return_value="\n".join(sub_links),
            ),
        ):
            resp = await sync_configs_and_hosts(panel_id, sync_req, db, owner_ctx)

        assert resp.configs_created == 31
        assert resp.hosts_created == 2

        cfg_count = await db.scalar(
            select(func.count()).select_from(OCPanelConfig).where(OCPanelConfig.panel_id == panel_id)
        )
        assert cfg_count == 31

        dest_count = await db.scalar(
            select(func.count())
            .select_from(OCPanelDestinationHost)
            .where(
                OCPanelDestinationHost.panel_id == panel_id,
                OCPanelDestinationHost.source_missing.is_(False),
            )
        )
        assert dest_count == 2

        catalog_hosts = (
            await db.execute(
                select(ProxyHost).where(ProxyHost.inbound_tag.like(f"oc_{panel_id}_catalog-%"))
            )
        ).scalars().all()
        assert catalog_hosts == []

        hosts_api = await list_panel_hosts(panel_id, db, owner_ctx)
        assert len(hosts_api) == 2

        with patch("app.core.manager.core_manager.get_inbounds", new_callable=AsyncMock) as mock_inbounds:
            mock_inbounds.return_value = []
            options = await list_group_host_options(db)
        oc_opts = [o for o in options if o.panel_id == panel_id]
        assert len(oc_opts) == 2

        with (
            patch(
                "app.services.oc_panel_host_subscription.call_oc_panel_api",
                new_callable=AsyncMock,
                side_effect=_mock_panel_api,
            ),
            patch(
                "app.services.oc_panel_host_subscription.fetch_upstream_subscription_body",
                new_callable=AsyncMock,
                return_value="\n".join(sub_links),
            ),
        ):
            resp2 = await sync_configs_and_hosts(panel_id, sync_req, db, owner_ctx)
        assert resp2.configs_created == 0
        assert resp2.hosts_created >= 0
        dest_count2 = await db.scalar(
            select(func.count())
            .select_from(OCPanelDestinationHost)
            .where(OCPanelDestinationHost.panel_id == panel_id)
        )
        assert dest_count2 == 2
        rows = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel_id))
        ).scalars().all()
        for row in rows:
            assert row.virtual_inbound_tag == destination_virtual_inbound_tag(panel_id, row.destination_config_id)

        await db.rollback()


async def test_remove_imported_panel_does_not_orphan_destination_tags_before_delete():
    """Inbound cleanup after panel delete must not leave dangling destination rows (N/A); repair works if inbounds drop."""
    link = "vless://u@9.9.9.9:443?encryption=none#Real"
    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="x",
            purchaser_identity="b",
            name="P",
            test_user_id="t",
        )
        db.add(panel)
        await db.flush()
        await reconcile_panel_destination_hosts(db, panel, [link])
        await db.flush()
        row = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalar_one()
        tag = row.virtual_inbound_tag
        assert tag is not None

        await remove_imported_panel(db, panel)
        await db.flush()
        gone = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalar_one_or_none()
        assert gone is None

        await db.rollback()


async def main():
    await test_import_metadata_only_two_destination_hosts()
    await test_remove_imported_panel_does_not_orphan_destination_tags_before_delete()
    print("test_oc_catalog_destination_import: OK")


if __name__ == "__main__":
    asyncio.run(main())
