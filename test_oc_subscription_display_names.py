"""OC subscription remark naming: Host display vs presentation template."""
from __future__ import annotations

import asyncio
import re
import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

from sqlalchemy import select

from app.db import GetDB
from app.db.models import Admin, Group, ProxyHost, ProxyInbound, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCPanelDestinationHost
from app.services.oc_host_display import DEFAULT_OC_HOST_DISPLAY_TEMPLATE, resolve_oc_subscription_remark_source
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import replace_link_remark
from app.services.oc_user_subscription_hosts import collect_oc_subscription_links_for_user


def _customer_link(uuid_val: str, remark: str, host: str = "customer.example") -> str:
    return f"vless://{uuid_val}@{host}:8443?encryption=none&security=tls#{remark}"


def _presentation_status() -> str:
    return "vless://disc@x:1?encryption=none#📡Status|✅|📆(∞ day)|(0 B-∞)📊"


def _presentation_user() -> str:
    return "vless://disc@x:1?encryption=none#👤v1.7|pg"


async def test_host_display_name_overrides_default_template():
    upstream = [
        _presentation_status(),
        _presentation_user(),
        _customer_link("cust-uuid", "🇸🇪|CDN"),
    ]
    catalog_id = "HTTP-UP"

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
        await reconcile_panel_destination_hosts(db, panel, upstream)
        dest = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalar_one()
        dest.display_name = "🇸🇪 CDN"
        assert dest.display_name_template == DEFAULT_OC_HOST_DISPLAY_TEMPLATE
        dest_tag = dest.virtual_inbound_tag
        inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == dest_tag))).scalar_one()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
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
        from app.db.models_oc import OCUserMapping

        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=[catalog_id],
                external_subscription_url="https://1.1.1.1/sub/x",
            )
        )
        await db.commit()
        uid = user.id

    body = "\n".join(upstream)
    with patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=body),
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(
                db, uid, [dest_tag], {"USERNAME": "bob"}, user_tenant_id=None
            )
    assert len(links) == 1
    remark = unquote(links[0].split("#", 1)[-1])
    assert "📡Status" not in remark
    assert "👤v1.7" not in remark
    assert remark == "🇸🇪 CDN"
    assert "cust-uuid" in links[0]


async def test_custom_template_still_renders_variables():
    rendered = resolve_oc_subscription_remark_source(
        display_name="🇸🇪 CDN",
        display_name_template="Flag {USERNAME}",
    )
    assert rendered == "Flag {USERNAME}"
    out = rendered.format_map({"USERNAME": "alice"})
    assert out == "Flag alice"


async def test_credentials_unchanged_when_remark_changes():
    raw = _customer_link("fixed-uuid-1234", "old-remark")
    new = replace_link_remark(raw, "🇸🇪 CDN")
    assert new.startswith("vless://fixed-uuid-1234@")
    assert "old-remark" not in new
    assert "🇸🇪" in new or "%F0" in new


async def test_three_configs_by_source_id_not_order():
    specs = [
        ("HTTP-UP", "🇸🇪 CDN", "c1", "🇸🇪|CDN"),
        ("Shadowsocks TCP", "🇸🇪 DIRECT", "c2", "🇸🇪|DIRECT"),
        ("VLESS XHHTT", "🇸🇪 XHTTP", "c3", "🇸🇪|XHTTP"),
    ]
    upstream = [_presentation_status(), _presentation_user()]
    for _, _, uuid_val, remark in specs:
        upstream.append(_customer_link(uuid_val, remark, host=f"{uuid_val}.example"))

    async with GetDB() as db:
        admin = (await db.execute(select(Admin).limit(1))).scalar_one()
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="sp", purchaser_identity="b", name="P")
        db.add(panel)
        await db.flush()
        for cid, _name, _u, _r in specs:
            tag = f"oc_{panel.id}_{cid}"
            db.add(ProxyInbound(tag=tag))
            await db.flush()
            db.add(
                OCPanelConfig(
                    panel_id=panel.id,
                    source_config_id=cid,
                    source_name=cid,
                    virtual_inbound_tag=tag,
                )
            )
        await db.flush()
        await reconcile_panel_destination_hosts(db, panel, upstream)
        dest_rows = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalars().all()
        by_sid = {}
        tags = []
        for row in dest_rows:
            sid = (row.source_payload or {}).get("oc_source_config_id")
            by_sid[sid] = row
            for cid, display, _u, _r in specs:
                if cid == sid:
                    row.display_name = display
            tags.append(row.virtual_inbound_tag)
        inbounds = []
        for t in tags:
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
        from app.db.models_oc import OCUserMapping

        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=[s[0] for s in specs],
                external_subscription_url="https://1.1.1.1/sub/x",
            )
        )
        await db.commit()
        uid = user.id

    body = "\n".join(upstream)
    with patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=body),
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(db, uid, tags, {}, user_tenant_id=None)

    assert len(links) == 3
    for cid, display, uuid_val, _r in specs:
        matched = [l for l in links if uuid_val in l]
        assert len(matched) == 1
        remark = unquote(matched[0].split("#", 1)[-1])
        assert "📡Status" not in remark
        assert remark == display


async def main():
    await test_host_display_name_overrides_default_template()
    await test_custom_template_still_renders_variables()
    await test_credentials_unchanged_when_remark_changes()
    await test_three_configs_by_source_id_not_order()
    print("test_oc_subscription_display_names: OK")


if __name__ == "__main__":
    asyncio.run(main())
