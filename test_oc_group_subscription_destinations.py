"""Group + subscription: panel destinations with NULL virtual_inbound_tag must render."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db import GetDB
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Group, ProxyHost, ProxyInbound, Tenant, TenantStatus, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost, OCUserMapping
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import destination_virtual_inbound_tag, share_link_config_base, vless_trojan_client_uuid
from app.services.oc_user_subscription_hosts import collect_oc_subscription_links_for_user
def _customer_link(uuid_val: str, remark: str) -> str:
    return f"vless://{uuid_val}@customer.example:8443?encryption=none&security=tls#{remark}"


async def test_group_subscription_includes_status_and_panel_destinations_null_vtags():
    discovery = [
        "vless://d1@1.1.1.1:443?encryption=none#%F0%9F%87%B8%F0%9F%87%AA%20CDN",
        "vless://d2@2.2.2.2:443?encryption=none#%F0%9F%87%B8%F0%9F%87%AA%20XHTTP",
        "vless://d3@3.3.3.3:443?encryption=none#%F0%9F%87%B8%F0%9F%87%AA%20DIRECT",
    ]
    customer = [
        _customer_link("c-cdn", "🇸🇪|CDN"),
        _customer_link("c-xhttp", "🇸🇪|XHTTP"),
        _customer_link("c-direct", "🇸🇪|DIRECT"),
    ]
    status_remark = "📡Status|{STATUS_EMOJI}|📆({DAYS_LEFT} day)|({DATA_USAGE}-{DATA_LIMIT})📊"
    status_tag = f"status_{uuid.uuid4().hex[:10]}"

    async with GetDB() as db:
        tenant = Tenant(name=f"gs_{uuid.uuid4().hex[:10]}", status=TenantStatus.active)
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
            name="Panel 86",
            tenant_id=tenant.id,
            workspace_id=workspace_id,
        )
        db.add(panel)
        await db.flush()

        await reconcile_panel_destination_hosts(db, panel, discovery)
        await db.flush()

        dest_rows = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalars().all()
        assert len(dest_rows) == 3
        dest_a, dest_b, dest_c = dest_rows[0], dest_rows[1], dest_rows[2]
        for row in dest_rows:
            row.virtual_inbound_tag = None
            payload = dict(row.source_payload or {})
            payload.pop("oc_source_config_id", None)
            row.source_payload = payload
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

        tag_a = destination_virtual_inbound_tag(panel.id, dest_a.destination_config_id)
        tag_b = destination_virtual_inbound_tag(panel.id, dest_b.destination_config_id)
        tag_c = destination_virtual_inbound_tag(panel.id, dest_c.destination_config_id)

        customer = [
            _customer_link("c-cdn", dest_a.display_name),
            _customer_link("c-xhttp", dest_b.display_name),
            _customer_link("c-direct", dest_c.display_name),
        ]

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

        inbounds = [status_inbound]
        for t in (tag_a, tag_b):
            oc_inbound = (
                await db.execute(select(ProxyInbound).where(ProxyInbound.tag == t))
            ).scalar_one_or_none()
            if oc_inbound is None:
                oc_inbound = ProxyInbound(tag=t)
                db.add(oc_inbound)
                await db.flush()
            inbounds.append(oc_inbound)

        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=inbounds)
        db.add(group)
        await db.flush()

        users: list[User] = []
        for suffix in ("one", "two"):
            user = User(
                username=f"u_{suffix}_{uuid.uuid4().hex[:6]}",
                status=UserStatus.active,
                admin_id=admin.id,
                workspace_id=workspace_id,
                proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            )
            user.groups = [group]
            db.add(user)
            await db.flush()
            db.add(
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel.id,
                    external_user_id=f"ext_{suffix}",
                    status="active",
                    last_synced_configs=[],
                    external_subscription_url="https://1.1.1.1/sub/token",
                )
            )
            users.append(user)
        await db.commit()
        user_ids = [u.id for u in users]

    body = "\n".join(customer)
    conn_patch = patch(
        "app.services.oc_subscription_runtime.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    )
    with conn_patch, patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=body),
    ):
        for uid in user_ids:
            async with GetDB() as db:
                links = await collect_oc_subscription_links_for_user(
                    db,
                    uid,
                    [tag_a, tag_b],
                    {"USERNAME": "alice", "STATUS_EMOJI": "✅", "DAYS_LEFT": "3", "DATA_USAGE": "1", "DATA_LIMIT": "10"},
                    user_tenant_id=tenant.id,
                )
            assert len(links) == 2
            joined = "\n".join(links)
            assert "c-cdn" in joined
            assert "c-xhttp" in joined
            assert "c-direct" not in joined
            assert "d1@" not in joined
            upstream_bases = {share_link_config_base(u) for u in customer}
            for final in links:
                assert share_link_config_base(final) in upstream_bases
                assert vless_trojan_client_uuid(final) in {
                    vless_trojan_client_uuid(u) for u in customer if vless_trojan_client_uuid(u)
                }

    with conn_patch, patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=body),
    ):
        async with GetDB() as db:
            orphan_tags = await collect_oc_subscription_links_for_user(
                db,
                user_ids[0],
                [tag_c],
                {},
                user_tenant_id=tenant.id,
            )
        assert len(orphan_tags) == 1
        assert "c-direct" in orphan_tags[0]

    with conn_patch, patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=body),
    ):
        async with GetDB() as db:
            core_only = await collect_oc_subscription_links_for_user(
                db,
                user_ids[0],
                ["Shadowsocks TCP"],
                {},
                user_tenant_id=tenant.id,
            )
        assert core_only == []


async def main():
    await test_group_subscription_includes_status_and_panel_destinations_null_vtags()
    print("test_oc_group_subscription_destinations: OK")


if __name__ == "__main__":
    asyncio.run(main())
