"""Destination subscription config → one OC Host."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.db import GetDB
from app.db.models import ProxyHost, ProxyInbound
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCPanelDestinationHost
from app.services.assignable_hosts import list_group_host_options
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import (
    destination_virtual_inbound_tag,
    subscription_config_links,
    subscription_link_uri_fingerprint,
)


def _sample_links(n: int) -> list[str]:
    out = []
    for i in range(n):
        out.append(f"vless://uuid{i}@host{i}.example:443?encryption=none#Config%20{i}")
    return out


async def test_one_host_per_subscription_config():
    links = _sample_links(5)
    assert len(subscription_config_links(links)) == 5

    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="1506", purchaser_identity="b", name="P")
        db.add(panel)
        await db.flush()
        for i in range(3):
            cid = f"cat-{i}"
            tag = f"oc_{panel.id}_{cid}"
            db.add(ProxyInbound(tag=tag))
            await db.flush()
            db.add(
                OCPanelConfig(
                    panel_id=panel.id,
                    source_config_id=cid,
                    source_name=f"HTTP-NAME-{i}",
                    virtual_inbound_tag=tag,
                )
            )
        await db.flush()
        count = await reconcile_panel_destination_hosts(db, panel, links)
        await db.commit()
        assert count == 5
        hosts = (
            await db.scalar(
                select(func.count())
                .select_from(OCPanelDestinationHost)
                .where(OCPanelDestinationHost.panel_id == panel.id, OCPanelDestinationHost.source_missing.is_(False))
            )
        )
        assert hosts == 5
        options = await list_group_host_options(db)
        oc_opts = [o for o in options if o.inbound_tag.startswith(f"oc_{panel.id}_d_")]
        assert len(oc_opts) == 5
        for o in oc_opts:
            assert "HTTP-NAME" not in o.display_name
            assert "HTTP-UP" not in o.display_name


async def test_stable_identity_survives_refresh():
    link = "vless://stable@1.2.3.4:443?encryption=none#%F0%9F%87%B3%F0%9F%87%B1%20NL"
    fp = subscription_link_uri_fingerprint(link)
    tag = destination_virtual_inbound_tag(99, fp)
    assert tag == f"oc_99_d_{fp}"

    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="sp", purchaser_identity="b", name="P")
        db.add(panel)
        await db.flush()
        await reconcile_panel_destination_hosts(db, panel, [link])
        await db.commit()
        row = (
            await db.execute(
                select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id)
            )
        ).scalar_one()
        assert row.destination_config_id == fp
        renamed = link.replace("NL", "NL%20Reality")
        await reconcile_panel_destination_hosts(db, panel, [renamed])
        await db.commit()
        row2 = (
            await db.execute(
                select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id)
            )
        ).scalar_one()
        assert row2.id == row.id
        assert row2.destination_config_id == fp


async def main():
    await test_one_host_per_subscription_config()
    await test_stable_identity_survives_refresh()
    print("test_oc_destination_hosts: OK")


if __name__ == "__main__":
    asyncio.run(main())
