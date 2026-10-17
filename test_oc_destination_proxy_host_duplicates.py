"""Subscription must not 500 when duplicate legacy ProxyHost rows match a destination remark."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.models import Admin, ProxyHost, Tenant, TenantStatus
from test_integration_admin import seed_tenant_admin_workspace
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost
from app.services.oc_destination_runtime import _proxy_host_for_destination
from app.services.oc_share_link import destination_virtual_inbound_tag, subscription_link_uri_fingerprint


@pytest.mark.asyncio
async def test_proxy_host_remark_fallback_tolerates_duplicates():
    discovery = "vless://disc@discovery.example:443?encryption=none#CDN"
    fp = subscription_link_uri_fingerprint(discovery)
    async with GetDB() as db:
        tenant_id, workspace_id, _admin_id = await seed_tenant_admin_workspace(db)

        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="sp",
            purchaser_identity="b",
            name="P",
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        db.add(panel)
        await db.flush()
        tag = destination_virtual_inbound_tag(panel.id, fp)
        dest = OCPanelDestinationHost(
            panel_id=panel.id,
            destination_config_id="cfg1",
            display_name="CDN Host",
            virtual_inbound_tag=None,
            source_missing=False,
            locally_hidden=False,
            source_payload={"subscription_link": discovery},
        )
        db.add(dest)
        await db.flush()

        for _ in range(2):
            db.add(
                ProxyHost(
                    remark="CDN Host",
                    port=443,
                    priority=0,
                    path=None,
                    status=[],
                    alpn=[],
                    is_disabled=False,
                    allowinsecure=False,
                    address={"discovery.example"},
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )
            )
        await db.flush()

        proxy = await _proxy_host_for_destination(db, panel, dest, tag)
        assert proxy is not None

        await db.rollback()
