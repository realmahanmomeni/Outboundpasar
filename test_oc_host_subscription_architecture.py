"""
Regression: OC hosts are subscription-backed, not destination inbound materializations.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

try:
    import pytest
except ImportError:
    pytest = None  # type: ignore

from app.db import GetDB
from app.db.models import Admin, Group, ProxyHost, ProxyInbound, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCUserMapping, OCSyncState
from app.node.oc_sync import enqueue_discovery_user_delete_sync, remove_imported_panel
from app.services.oc_host_display import DEFAULT_OC_HOST_DISPLAY_TEMPLATE
from app.services.oc_panel_materialization import (
    upsert_oc_panel_config_row,
    virtual_inbound_tag,
)
from app.services.oc_share_link import parse_share_link
from app.services.oc_user_subscription_hosts import collect_oc_subscription_links_for_user


async def test_materialization_does_not_seed_placeholder_inbound_address():
    async with GetDB() as db:
        intg = OCIntegration(
            base_url="https://oc.example",
            api_token_encrypted="e",
            token_preview="t",
        )
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="1506",
            purchaser_identity="buyer",
            name="panel",
        )
        db.add(panel)
        await db.flush()
        c_data = {
            "id": "inbound-tag-1",
            "name": "inbound-tag-1",
            "protocol": "vless",
            "network": "tcp",
            "group_mapping": {"supported": False},
        }
        _, _, _ = await upsert_oc_panel_config_row(
            db,
            panel=panel,
            c_data=c_data,
            existing_configs={},
            existing_group_by_source={},
        )
        await db.commit()
        cfg = (
            await db.execute(
                select(OCPanelConfig).where(OCPanelConfig.panel_id == panel.id)
            )
        ).scalar_one()
        legacy_tag = virtual_inbound_tag(panel.id, "inbound-tag-1")
        host = (
            await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == legacy_tag))
        ).scalar_one_or_none()
        assert host is None
        assert cfg.locally_hidden is True
        assert cfg.virtual_inbound_tag is None
        assert cfg.protocol == "vless"
        assert cfg.display_name_template == DEFAULT_OC_HOST_DISPLAY_TEMPLATE


async def test_user_subscription_link_replaces_remark_template():
    link = "vless://u@94.1.1.1:443?encryption=none#cfg-a"
    parsed = parse_share_link(link)
    assert parsed is not None
    assert parsed.address == "94.1.1.1"

    async with GetDB() as db:
        admin = (await db.execute(select(Admin).limit(1))).scalar_one()
        intg = OCIntegration(
            base_url="https://oc.example",
            api_token_encrypted="e",
            token_preview="t",
        )
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="sp", purchaser_identity="t", name="P")
        db.add(panel)
        await db.flush()
        tag = virtual_inbound_tag(panel.id, "cfg-a")
        inbound = ProxyInbound(tag=tag)
        db.add(inbound)
        await db.flush()
        host = ProxyHost(
            remark="cfg-a",
            priority=0,
            address=set(),
            port=None,
            path=None,
            allowinsecure=None,
            alpn=[],
            status=[],
        )
        host.inbound = inbound
        db.add(host)
        await db.flush()
        db.add(
            OCPanelConfig(
                panel_id=panel.id,
                source_config_id="cfg-a",
                source_name="cfg-a",
                virtual_inbound_tag=tag,
                display_name_template="Hi {USERNAME}",
            )
        )
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_tpl_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            sub_token=uuid.uuid4().hex,
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
                last_synced_configs=["cfg-a"],
                external_subscription_url="https://1.1.1.1/sub/tok",
            )
        )
        await db.commit()
        uid = user.id

    with patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=link),
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(
                db,
                uid,
                [tag],
                {"USERNAME": "alice"},
                user_tenant_id=None,
            )
    assert len(links) == 1
    assert "94.1.1.1" in links[0]
    assert links[0].endswith("#Hi%20alice") or "#Hi alice" in links[0]


async def test_panel_delete_enqueues_discovery_user_removal():
    async with GetDB() as db:
        intg = OCIntegration(
            base_url="https://oc.example",
            api_token_encrypted="enc",
            token_preview="prev",
            is_active=True,
        )
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="1507",
            purchaser_identity="1",
            name="p",
            test_user_id="pasarguard_discovery_1507",
        )
        db.add(panel)
        await db.flush()
        pid = panel.id
        await enqueue_discovery_user_delete_sync(db, panel)
        await db.commit()
        job = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_type == "panel_discovery_user",
                    OCSyncState.entity_id == str(pid),
                )
            )
        ).scalar_one()
        assert job.payload["external_user_id"] == "pasarguard_discovery_1507"
        assert job.operation == "delete"
        await remove_imported_panel(db, panel)
        await db.commit()
        assert (
            await db.execute(select(OCPanel).where(OCPanel.id == pid))
        ).scalar_one_or_none() is None


async def test_host_name_is_not_user_template():
    from app.services.oc_host_display import remark_looks_like_user_template, resolve_oc_host_display_name

    assert remark_looks_like_user_template(DEFAULT_OC_HOST_DISPLAY_TEMPLATE)
    assert not remark_looks_like_user_template("HTTP-UP")

    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="sp", purchaser_identity="t", name="P")
        db.add(panel)
        await db.flush()
        tag = virtual_inbound_tag(panel.id, "HTTP-UP")
        inbound = ProxyInbound(tag=tag)
        db.add(inbound)
        await db.flush()
        host = ProxyHost(
            remark=DEFAULT_OC_HOST_DISPLAY_TEMPLATE,
            priority=0,
            address=set(),
            port=None,
            path=None,
            allowinsecure=None,
            alpn=[],
            status=[],
        )
        host.inbound = inbound
        db.add(host)
        cfg = OCPanelConfig(
            panel_id=panel.id,
            source_config_id="HTTP-UP",
            source_name="HTTP-UP",
            virtual_inbound_tag=tag,
            display_name_template=DEFAULT_OC_HOST_DISPLAY_TEMPLATE,
        )
        db.add(cfg)
        await db.flush()
        assert resolve_oc_host_display_name(host, cfg) == "HTTP-UP"


async def main():
    await test_materialization_does_not_seed_placeholder_inbound_address()
    await test_user_subscription_link_replaces_remark_template()
    await test_host_name_is_not_user_template()
    await test_panel_delete_enqueues_discovery_user_removal()
    print("test_oc_host_subscription_architecture: OK")


if __name__ == "__main__":
    asyncio.run(main())
