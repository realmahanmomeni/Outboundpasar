"""
Regression: panel removal must delete inbounds_groups_association before bulk inbound DELETE.

Bulk DELETE bypasses ProxyInbound after_delete, which caused FK violations and HTTP 503 on DELETE /api/panels.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, select

from app.db import GetDB
from app.db.models import Group, ProxyHost, ProxyInbound, inbounds_groups_association
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig
from fastapi import HTTPException

from app.db.models import Tenant
from app.models.admin import AdminDetails, AdminRoleData, AdminStatus
from app.models.admin_role import CRUDPermissions, RolePermissions
from app.node.oc_sync import remove_imported_panel
from app.routers.panel import delete_panel


async def test_remove_imported_panel_clears_inbound_group_associations():
    tag = f"oc_pd_grp_{uuid.uuid4().hex[:10]}"
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
            source_panel_id=f"src_{uuid.uuid4().hex[:6]}",
            purchaser_identity="admin",
            name="grp-panel",
        )
        db.add(panel)
        await db.flush()

        inbound = ProxyInbound(tag=tag)
        db.add(inbound)
        await db.flush()

        group = Group(name=f"g_{uuid.uuid4().hex[:8]}", inbounds=[inbound])
        db.add(group)
        await db.flush()

        host = ProxyHost(
            remark="host",
            priority=0,
            address={"1.2.3.4"},
            port=443,
            path=None,
            status=[],
            alpn=[],
            is_disabled=False,
            allowinsecure=False,
        )
        host.inbound = inbound
        db.add(host)

        db.add(
            OCPanelConfig(
                panel_id=panel.id,
                source_config_id="cfg1",
                source_name="cfg",
                virtual_inbound_tag=tag,
            )
        )
        await db.flush()

        panel_id = panel.id
        inbound_id = inbound.id

        await remove_imported_panel(db, panel)
        await db.flush()

        assert (await db.execute(select(OCPanel).where(OCPanel.id == panel_id))).scalar_one_or_none() is None
        assert (await db.execute(select(ProxyInbound).where(ProxyInbound.id == inbound_id))).scalar_one_or_none() is None
        assoc = (
            await db.execute(
                select(inbounds_groups_association).where(
                    inbounds_groups_association.c.inbound_id == inbound_id
                )
            )
        ).first()
        assert assoc is None

        await db.rollback()


async def test_delete_panel_cross_tenant_admin_gets_403():
    async with GetDB() as db:
        tenant = Tenant(name=f"pd_del_{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.flush()

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
            source_panel_id=f"other_{uuid.uuid4().hex[:6]}",
            purchaser_identity="owner",
            name="foreign",
            tenant_id=None,
        )
        db.add(panel)
        await db.flush()
        panel_id = panel.id

        admin = AdminDetails(
            id=1,
            username="tenant_admin",
            tenant_id=tenant.id,
            status=AdminStatus.active,
            is_sudo=False,
            role=AdminRoleData(
                id=1,
                name="administrator",
                is_owner=False,
                permissions=RolePermissions(nodes=CRUDPermissions(read=True, update=True)),
            ),
        )
        ctx = (admin.username, False, admin)
        try:
            await delete_panel(panel_id, db=db, user_context=ctx)
            raise AssertionError("expected 403")
        except HTTPException as exc:
            assert exc.status_code == 404

        await db.rollback()


async def main():
    await test_remove_imported_panel_clears_inbound_group_associations()
    await test_delete_panel_cross_tenant_admin_gets_403()
    print("test_panel_delete_inbound_groups: OK")


if __name__ == "__main__":
    asyncio.run(main())
