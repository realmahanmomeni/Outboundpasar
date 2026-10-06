"""OC user sync must stay active when group tags use destination hosts without catalog id mapping."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db import GetDB
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Group, ProxyInbound, Tenant, TenantStatus, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost, OCUserMapping, OCSyncState
from app.node.oc_sync import enqueue_oc_user_sync
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import destination_virtual_inbound_tag


async def test_enqueue_sync_keeps_mapping_for_destination_tags_without_catalog_ids():
    discovery = [
        "vless://u1@1.1.1.1:443?encryption=none#%F0%9F%87%B8%F0%9F%87%AA%20Marketing%20Name",
    ]

    async with GetDB() as db:
        tenant = Tenant(name=f"t_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
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
            source_panel_id="1507",
            purchaser_identity="buyer",
            name="Panel",
            tenant_id=tenant.id,
            workspace_id=workspace_id,
            sync_status="connected",
        )
        db.add(panel)
        await db.flush()

        await reconcile_panel_destination_hosts(db, panel, discovery)
        await db.flush()
        dest = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalar_one()
        tag = destination_virtual_inbound_tag(panel.id, dest.destination_config_id)
        inbound = (
            await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))
        ).scalar_one()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound], tenant_id=tenant.id, workspace_id=workspace_id)
        db.add(group)
        await db.flush()

        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=workspace_id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()

        with patch(
            "app.node.oc_sync.tenant_panel_allows_oc_user_mutations",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await enqueue_oc_user_sync(db, user)
        await db.flush()

        mapping = (
            await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == user.id, OCUserMapping.panel_id == panel.id)
            )
        ).scalar_one_or_none()
        assert mapping is not None
        jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_type == "user_mapping",
                    OCSyncState.entity_id == f"{user.id}_{panel.id}",
                )
            )
        ).scalars().all()
        assert jobs, "expected OC sync job when destination host tags lack catalog config ids"
        assert jobs[0].operation in ("create", "update")

        await db.rollback()
