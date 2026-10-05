"""Imported OC destination Host edit: display name only."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db import GetDB
from app.db.models import ProxyHost, ProxyInbound
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost
from test_integration_admin import owner_admin_details
from app.models.host import CreateHost
from app.operation import OperatorType
from app.operation.host import HostOperation
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import destination_virtual_inbound_tag, subscription_link_uri_fingerprint


async def test_oc_destination_host_modify_rejects_technical_changes():
    link = "vless://u@9.9.9.9:443?encryption=none#Germany%2001"
    fp = subscription_link_uri_fingerprint(link)
    assert fp

    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="sp", purchaser_identity="b", name="P")
        db.add(panel)
        await db.flush()
        await reconcile_panel_destination_hosts(db, panel, [link])
        await db.commit()

        tag = destination_virtual_inbound_tag(panel.id, fp)
        host = (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == tag))).scalar_one()
        op = HostOperation(operator_type=OperatorType.API)
        admin = owner_admin_details(admin_id=1, username="admin")

        mutated = CreateHost.model_validate(host)
        mutated.remark = "🇩🇪 Germany 01"
        mutated.address = {"1.2.3.4"}
        mutated.port = 80
        result = await op.modify_host(db, host.id, mutated, admin)
        await db.commit()

        assert result.remark == "🇩🇪 Germany 01"
        refreshed = (await db.execute(select(ProxyHost).where(ProxyHost.id == host.id))).scalar_one()
        assert "9.9.9.9" in (refreshed.address or set())
        assert refreshed.port == 443
        dest = (
            await db.execute(
                select(OCPanelDestinationHost).where(
                    OCPanelDestinationHost.panel_id == panel.id,
                    OCPanelDestinationHost.destination_config_id == fp,
                )
            )
        ).scalar_one()
        assert dest.display_name == "🇩🇪 Germany 01"


async def main():
    await test_oc_destination_host_modify_rejects_technical_changes()
    print("test_oc_host_edit_restrictions: OK")


if __name__ == "__main__":
    asyncio.run(main())
