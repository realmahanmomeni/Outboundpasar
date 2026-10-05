"""Group host picker inventory: destination OC Hosts, not destination inbounds."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db import GetDB
from app.db.models import ProxyHost, ProxyInbound, Tenant, TenantStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCPanelDestinationHost
from app.services.assignable_hosts import list_group_host_options
from app.services.oc_inbound_tags import list_assignable_inbound_tags, validate_inbound_tags_for_group
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts


async def test_group_options_exclude_legacy_catalog_and_inbounds_api():
    links = ["vless://u@1.1.1.1:443?encryption=none#%F0%9F%87%B3%F0%9F%87%B1%20NL%20Reality"]

    async with GetDB() as db:
        tenant = Tenant(name=f"picker_{uuid.uuid4().hex[:10]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="1507",
            purchaser_identity="b",
            name="P",
            tenant_id=tenant.id,
        )
        db.add(panel)
        await db.flush()

        legacy_tag = f"oc_{panel.id}_HTTP-UP"
        db.add(
            OCPanelConfig(
                panel_id=panel.id,
                source_config_id="HTTP-UP",
                source_name="HTTP-UP",
                virtual_inbound_tag=legacy_tag,
            )
        )
        legacy_inbound = ProxyInbound(tag=legacy_tag)
        db.add(legacy_inbound)
        await db.flush()
        legacy_host = ProxyHost(
            remark="HTTP-UP",
            priority=0,
            address={"8.8.8.8"},
            port=None,
            path=None,
            status=[],
            alpn=[],
            is_disabled=False,
            allowinsecure=False,
            tenant_id=tenant.id,
        )
        legacy_host.inbound = legacy_inbound
        db.add(legacy_host)
        await db.flush()

        await reconcile_panel_destination_hosts(db, panel, links)
        await db.commit()

        with patch("app.core.manager.core_manager.get_inbounds", new_callable=AsyncMock) as mock_inbounds:
            mock_inbounds.return_value = ["VLESS TCP", "inbound-1"]
            inbound_api_tags = await list_assignable_inbound_tags(db)
            assert "HTTP-UP" not in inbound_api_tags
            assert legacy_tag not in inbound_api_tags
            assert "VLESS TCP" in inbound_api_tags

        options = await list_group_host_options(db, tenant_id=tenant.id, workspace_id=None)
        tags = {o.inbound_tag for o in options}
        names = {o.display_name for o in options}
        assert legacy_tag not in tags
        assert "HTTP-UP" not in names
        assert "VLESS TCP" not in names
        oc_opts = [o for o in options if o.inbound_tag.startswith(f"oc_{panel.id}_d_")]
        assert len(oc_opts) == 1
        assert oc_opts[0].display_name == "🇳🇱 NL Reality"
        assert oc_opts[0].is_oc_destination is True
        assert oc_opts[0].destination_config_id

        errors: list[str] = []

        async def raise_error(msg, code, db=None):
            errors.append(msg)

        await validate_inbound_tags_for_group(db, [oc_opts[0].inbound_tag], raise_error=raise_error)
        assert not errors
        await validate_inbound_tags_for_group(db, [legacy_tag], raise_error=raise_error)
        assert errors


async def main():
    await test_group_options_exclude_legacy_catalog_and_inbounds_api()
    print("test_oc_group_picker_inventory: OK")


if __name__ == "__main__":
    asyncio.run(main())
