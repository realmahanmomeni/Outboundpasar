"""Regression: multi-panel groups when one upstream subscription leads with ss:// links."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db import GetDB
from app.db.models import Admin, Group, ProxyHost, ProxyInbound, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost, OCUserMapping
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import (
    decode_subscription_links,
    destination_virtual_inbound_tag,
    share_link_config_base,
)
from app.services.oc_upstream_subscription import _decode_links_payload
from app.services.oc_user_subscription_hosts import collect_oc_subscription_links_for_user


def test_decode_plaintext_subscription_leading_with_ss():
    body = (
        "ss://Y2hhY2hhMjAtaWV0Zi1wb2x5MTMwNTpQQVNT@host:1080#Status\n"
        "vless://11111111-1111-1111-1111-111111111111@1.1.1.1:443?encryption=none#PanelA-HTTP\n"
        "vless://22222222-2222-2222-2222-222222222222@2.2.2.2:443?encryption=none#PanelA-SS\n"
    )
    lines = _decode_links_payload(body)
    assert len(lines) == 3
    decoded = decode_subscription_links(body)
    assert len(decoded) == 3
    assert any("1.1.1.1" in link for link in decoded)


async def test_multi_panel_group_collects_both_when_one_panel_uses_ss_leading_sub():
    discovery_a = [
        "vless://disc-a1@discovery-a.example:443?encryption=none#HTTP-UP",
        "vless://disc-a2@discovery-a.example:443?encryption=none#Shadowsocks%20TCP",
    ]
    discovery_b = [
        "vless://disc-b1@discovery-b.example:443?encryption=none#direct",
    ]
    customer_a = (
        "ss://Y2hhY2hhMjAtaWV0Zi1wb2x5MTMwNTpQQVNT@status-a:1080#Status\n"
        "vless://cust-a1@panel-a.example:8443?encryption=none&security=tls#HTTP-UP\n"
        "vless://cust-a2@panel-a.example:8443?encryption=none&security=tls#Shadowsocks%20TCP\n"
    )
    customer_b = "vless://cust-b1@panel-b.example:8443?encryption=none&security=tls#direct\n"

    async with GetDB() as db:
        admin = (await db.execute(select(Admin).limit(1))).scalar_one()
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()

        panel_a = OCPanel(
            integration_id=intg.id,
            source_panel_id="1506",
            purchaser_identity="mp",
            name="Panel A",
        )
        panel_b = OCPanel(
            integration_id=intg.id,
            source_panel_id="1507",
            purchaser_identity="mp",
            name="Panel B",
        )
        db.add_all([panel_a, panel_b])
        await db.flush()

        await reconcile_panel_destination_hosts(db, panel_a, discovery_a)
        await reconcile_panel_destination_hosts(db, panel_b, discovery_b)
        await db.flush()

        dest_a_rows = (
            await db.execute(
                select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel_a.id)
            )
        ).scalars().all()
        dest_b_rows = (
            await db.execute(
                select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel_b.id)
            )
        ).scalars().all()
        assert len(dest_a_rows) == 2
        assert len(dest_b_rows) == 1

        tags_a = [destination_virtual_inbound_tag(panel_a.id, r.destination_config_id) for r in dest_a_rows]
        tags_b = [destination_virtual_inbound_tag(panel_b.id, r.destination_config_id) for r in dest_b_rows]
        all_tags = tags_a + tags_b

        inbounds: list[ProxyInbound] = []
        for tag in all_tags:
            inbound = (
                await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))
            ).scalar_one_or_none()
            if inbound is None:
                inbound = ProxyInbound(tag=tag)
                db.add(inbound)
                await db.flush()
            inbounds.append(inbound)

        group = Group(name=f"mp_{uuid.uuid4().hex[:8]}", inbounds=inbounds)
        db.add(group)
        await db.flush()

        user = User(
            username=f"u_mp_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()

        url_a = "https://1.0.0.1/sub/panel-a-token"
        url_b = "https://1.0.0.2/sub/panel-b-token"
        db.add_all(
            [
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel_a.id,
                    external_user_id="ext-a",
                    status="active",
                    last_synced_configs=["HTTP-UP", "Shadowsocks TCP"],
                    external_subscription_url=url_a,
                ),
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel_b.id,
                    external_user_id="ext-b",
                    status="active",
                    last_synced_configs=["direct"],
                    external_subscription_url=url_b,
                ),
            ]
        )
        await db.commit()
        uid = user.id

    async def _fake_fetch(url: str, _fmt: str, **kwargs):
        if "panel-a" in url:
            return customer_a
        if "panel-b" in url:
            return customer_b
        raise AssertionError(f"unexpected subscription url {url}")

    conn_patch = patch(
        "app.services.oc_subscription_runtime.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    )
    with conn_patch, patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(side_effect=_fake_fetch),
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(
                db,
                uid,
                all_tags,
                {"USERNAME": "alice", "STATUS_EMOJI": "✅", "DAYS_LEFT": "3", "DATA_USAGE": "1", "DATA_LIMIT": "10"},
                user_tenant_id=None,
            )

    assert len(links) == 3
    joined = "\n".join(links)
    assert "panel-a.example" in joined
    assert "panel-b.example" in joined
    bases = {share_link_config_base(link) for link in links}
    assert any("panel-a.example" in b for b in bases)
    assert any("panel-b.example" in b for b in bases)


async def _run():
    test_decode_plaintext_subscription_leading_with_ss()
    await test_multi_panel_group_collects_both_when_one_panel_uses_ss_leading_sub()
    print("test_oc_multi_panel_subscription_decode: OK")


if __name__ == "__main__":
    asyncio.run(_run())
