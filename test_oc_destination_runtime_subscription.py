"""OC destination host runtime subscription: stable source-config identity."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db import GetDB
from app.db.models import Admin, Group, ProxyHost, ProxyInbound, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCPanelDestinationHost, OCUserMapping
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import (
    destination_virtual_inbound_tag,
    match_upstream_link_for_oc_source_config,
    subscription_link_uri_fingerprint,
)
from app.services.oc_subscription_runtime import filter_oc_inbound_tags_for_runtime_subscription
from app.services.oc_user_subscription_hosts import collect_oc_subscription_links_for_user


def _discovery_link(uuid_val: str, remark: str) -> str:
    return f"vless://{uuid_val}@discovery.example:443?encryption=none#{remark}"


def _customer_link(uuid_val: str, remark: str) -> str:
    return f"vless://{uuid_val}@customer.example:8443?encryption=none&security=tls#{remark}"


async def test_runtime_tag_filter_resolves_destination_hosts():
    discovery = _discovery_link("disc-uuid", "HTTP-UP")
    fp = subscription_link_uri_fingerprint(discovery)
    catalog_id = "inbound-http-up"

    async with GetDB() as db:
        admin = (await db.execute(select(Admin).limit(1))).scalar_one()
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="sp", purchaser_identity="b", name="P")
        db.add(panel)
        await db.flush()
        tag = f"oc_{panel.id}_{catalog_id}"
        db.add(ProxyInbound(tag=tag))
        await db.flush()
        db.add(
            OCPanelConfig(
                panel_id=panel.id,
                source_config_id=catalog_id,
                source_name="HTTP-UP",
                virtual_inbound_tag=tag,
            )
        )
        await db.flush()
        await reconcile_panel_destination_hosts(db, panel, [discovery])
        await db.flush()
        dest = (
            await db.execute(
                select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id)
            )
        ).scalar_one()
        dest_tag = dest.virtual_inbound_tag
        assert dest_tag == destination_virtual_inbound_tag(panel.id, fp)

        dest_inbound = (
            await db.execute(select(ProxyInbound).where(ProxyInbound.tag == dest_tag))
        ).scalar_one()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[dest_inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=[catalog_id],
            )
        )
        await db.commit()
        uid = user.id

    async with GetDB() as db:
        filtered = await filter_oc_inbound_tags_for_runtime_subscription(
            db, uid, [dest_tag], user_tenant_id=None
        )
    assert filtered == [dest_tag]


async def test_customer_uuid_differs_from_discovery():
    catalog_id = "cfg-tunnel"
    discovery = _discovery_link("uuid-discovery", "Tunnel-FR")
    customer = _customer_link("uuid-customer", "Tunnel-FR")
    assert subscription_link_uri_fingerprint(discovery) != subscription_link_uri_fingerprint(customer)

    matched = match_upstream_link_for_oc_source_config(
        [customer],
        catalog_id,
        source_name="Tunnel-FR",
    )
    assert matched == customer
    assert "uuid-customer" in matched
    assert "uuid-discovery" not in matched


async def test_duplicate_remark_fails_closed():
    a = _customer_link("u1", "Same Name")
    b = _customer_link("u2", "Same Name")
    assert match_upstream_link_for_oc_source_config([a, b], "cfg-a", source_name="Same Name") is None


async def test_duplicate_remark_same_client_uuid_allowed():
    a = _customer_link("u1", "Same Name")
    b = _customer_link("u1", "Same Name")
    matched = match_upstream_link_for_oc_source_config([a, b], "cfg-a", source_name="Same Name")
    assert matched == a


async def test_collect_subscription_selected_configs_only():
    catalog_a, catalog_b, catalog_c = "Name-A", "Name-B", "Name-C"
    discovery_links = [
        _discovery_link("d1", "Name-A"),
        _discovery_link("d2", "Name-B"),
        _discovery_link("d3", "Name-C"),
    ]
    customer_links = [
        _customer_link("c1", "Name-A"),
        _customer_link("c2", "Name-B"),
        _customer_link("c3", "Name-C"),
    ]

    async with GetDB() as db:
        admin = (await db.execute(select(Admin).limit(1))).scalar_one()
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="sp", purchaser_identity="b", name="P")
        db.add(panel)
        await db.flush()
        for cid, name in ((catalog_a, catalog_a), (catalog_b, catalog_b), (catalog_c, catalog_c)):
            tag = f"oc_{panel.id}_{cid}"
            db.add(ProxyInbound(tag=tag))
            await db.flush()
            db.add(
                OCPanelConfig(
                    panel_id=panel.id,
                    source_config_id=cid,
                    source_name=name,
                    virtual_inbound_tag=tag,
                )
            )
        await db.flush()
        await reconcile_panel_destination_hosts(db, panel, discovery_links)
        await db.flush()
        dest_rows = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalars().all()
        by_catalog = {}
        for row in dest_rows:
            payload = row.source_payload or {}
            sid = payload.get("oc_source_config_id")
            by_catalog[sid] = row.virtual_inbound_tag

        tag_a = by_catalog[catalog_a]
        tag_c = by_catalog[catalog_c]
        inbounds = []
        for t in (tag_a, tag_c):
            inbounds.append((await db.execute(select(ProxyInbound).where(ProxyInbound.tag == t))).scalar_one())
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=inbounds)
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=[catalog_a, catalog_b, catalog_c],
                external_subscription_url="https://1.1.1.1/sub/x",
            )
        )
        dest_a = next(r for r in dest_rows if (r.source_payload or {}).get("oc_source_config_id") == catalog_a)
        dest_a.display_name_template = "Flag {USERNAME}"
        await db.commit()
        uid = user.id
        selected_tags = [tag_a, tag_c]

    body = "\n".join(customer_links)
    with patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=body),
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(
                db,
                uid,
                selected_tags,
                {"USERNAME": "bob"},
                user_tenant_id=None,
            )
    assert len(links) == 2
    joined = "\n".join(links)
    assert "c1" in joined
    assert "c3" in joined
    assert "c2" not in joined
    assert "customer.example" in joined
    assert "discovery.example" not in joined
    assert any("Flag%20bob" in link or "Flag bob" in link for link in links)


async def test_multi_panel_aggregation():
    customer_p1 = _customer_link("p1-u", "P1-CFG")
    customer_p2 = _customer_link("p2-u", "P2-CFG")

    async with GetDB() as db:
        admin = (await db.execute(select(Admin).limit(1))).scalar_one()
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panels = []
        dest_tags = []
        for idx, (cid, remark, cust) in enumerate(
            (
                ("P1-CFG", "P1-CFG", customer_p1),
                ("P2-CFG", "P2-CFG", customer_p2),
            ),
            start=1,
        ):
            panel = OCPanel(
                integration_id=intg.id,
                source_panel_id=f"sp{idx}",
                purchaser_identity="b",
                name=f"P{idx}",
            )
            db.add(panel)
            await db.flush()
            panels.append((panel, cust))
            tag = f"oc_{panel.id}_{cid}"
            db.add(ProxyInbound(tag=tag))
            await db.flush()
            db.add(
                OCPanelConfig(
                    panel_id=panel.id,
                    source_config_id=cid,
                    source_name=remark,
                    virtual_inbound_tag=tag,
                )
            )
            await db.flush()
            await reconcile_panel_destination_hosts(db, panel, [_discovery_link(f"d{idx}", remark)])
            dest = (
                await db.execute(
                    select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id)
                )
            ).scalar_one()
            dest_tags.append(dest.virtual_inbound_tag)

        inbounds = []
        for t in dest_tags:
            inbounds.append((await db.execute(select(ProxyInbound).where(ProxyInbound.tag == t))).scalar_one())
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=inbounds)
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        for panel, _cust in panels:
            db.add(
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel.id,
                    external_user_id=f"ext-{panel.id}",
                    status="active",
                    last_synced_configs=[panel.id and "x"],
                    external_subscription_url=f"https://1.1.1.1/sub/{panel.id}",
                )
            )
        await db.commit()
        uid = user.id
        p1, p2 = panels[0][0], panels[1][0]
        # fix synced configs
        async with GetDB() as db:
            for panel, cid in ((p1, "P1-CFG"), (p2, "P2-CFG")):
                m = (
                    await db.execute(
                        select(OCUserMapping).where(
                            OCUserMapping.user_id == uid, OCUserMapping.panel_id == panel.id
                        )
                    )
                ).scalar_one()
                m.last_synced_configs = [cid]
            await db.commit()

    async def fake_fetch(url, fmt):
        if f"/sub/{p1.id}" in url:
            return customer_p1
        return customer_p2

    with patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(side_effect=fake_fetch),
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(
                db,
                uid,
                dest_tags,
                {},
                user_tenant_id=None,
            )
    assert len(links) == 2
    assert any("p1-u" in link for link in links)
    assert any("p2-u" in link for link in links)


async def main():
    await test_runtime_tag_filter_resolves_destination_hosts()
    await test_customer_uuid_differs_from_discovery()
    await test_duplicate_remark_fails_closed()
    await test_collect_subscription_selected_configs_only()
    await test_multi_panel_aggregation()
    print("test_oc_destination_runtime_subscription: OK")


if __name__ == "__main__":
    asyncio.run(main())
