"""Group picker: imported panel destination configs + native hosts; not orphan core inbounds."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db import GetDB
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, ProxyHost, ProxyInbound, Tenant, TenantStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost
from app.services.assignable_hosts import list_group_host_options
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import destination_virtual_inbound_tag


async def test_panel_import_configs_visible_with_null_virtual_tags():
    links = [
        "vless://u@1.1.1.1:443?encryption=none#Config%20A",
        "ss://YWVzLTI1Ni1nY206dGVzdA@2.2.2.2:8388#Shadowsocks%20Panel%20Cfg",
        "vless://u@3.3.3.3:443?encryption=none#Config%20C",
    ]
    status_remark = "📡Status|{STATUS_EMOJI}|📆({DAYS_LEFT} day)|({DATA_USAGE}-{DATA_LIMIT})📊"

    async with GetDB() as db:
        tenant = Tenant(name=f"gp_{uuid.uuid4().hex[:10]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin = Admin(
            username=f"adm_{uuid.uuid4().hex[:8]}",
            hashed_password="x",
            role_id=role.id,
            tenant_id=tenant.id,
        )
        db.add(admin)
        await db.flush()
        await create_workspace_for_administrator(db, admin)
        workspace_id = admin.workspace_id
        assert workspace_id is not None

        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="1506",
            purchaser_identity="b",
            name="Panel",
            tenant_id=tenant.id,
            workspace_id=workspace_id,
        )
        db.add(panel)
        await db.flush()

        await reconcile_panel_destination_hosts(db, panel, links)
        await db.flush()

        dest_rows = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalars().all()
        assert len(dest_rows) == 3

        for row in dest_rows:
            row.virtual_inbound_tag = None
            host = (
                await db.execute(
                    select(ProxyHost).where(
                        ProxyHost.workspace_id == workspace_id,
                        ProxyHost.remark == row.display_name,
                    )
                )
            ).scalar_one_or_none()
            if host is not None:
                host.inbound_tag = None
                host.inbound_id = None
        await db.flush()

        status_tag = f"status_{uuid.uuid4().hex[:10]}"
        status_inbound = ProxyInbound(tag=status_tag)
        db.add(status_inbound)
        await db.flush()
        status_host = ProxyHost(
            remark=status_remark,
            priority=0,
            address={"127.0.0.1"},
            port=None,
            path=None,
            status=[],
            alpn=[],
            is_disabled=False,
            allowinsecure=False,
            tenant_id=tenant.id,
            workspace_id=workspace_id,
        )
        status_host.inbound = status_inbound
        db.add(status_host)
        await db.flush()

        core_only = f"core_orphan_{uuid.uuid4().hex[:8]}"
        with patch("app.core.manager.core_manager.get_inbounds", new_callable=AsyncMock) as mock_core:
            mock_core.return_value = [core_only, status_tag]
            options = await list_group_host_options(db, tenant_id=tenant.id, workspace_id=workspace_id)

        names = {o.display_name for o in options}
        tags = {o.inbound_tag for o in options}

        assert "Config A" in names
        assert "Config C" in names
        assert "Shadowsocks Panel Cfg" in names
        assert status_remark in names
        assert core_only not in tags

        for row in dest_rows:
            expected_tag = destination_virtual_inbound_tag(panel.id, row.destination_config_id)
            assert expected_tag in tags

        await db.rollback()


async def main():
    await test_panel_import_configs_visible_with_null_virtual_tags()
    print("test_oc_group_picker_panel_import: OK")


if __name__ == "__main__":
    asyncio.run(main())
