"""Panel destination subscription output must preserve upstream config UUIDs (no OC generation)."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.db import GetDB
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Tenant, TenantStatus, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost, OCUserMapping
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import (
    destination_virtual_inbound_tag,
    share_link_config_base,
    vless_trojan_client_uuid,
)
from app.services.oc_user_subscription_hosts import collect_oc_subscription_links_for_user


def _vless(uuid_val: str, host: str, port: int, remark: str) -> str:
    return f"vless://{uuid_val}@{host}:{port}?encryption=none&security=tls#{remark}"


async def test_upstream_uuid_and_config_base_preserved_two_users():
    discovery = [
        "vless://disc-cdn@discovery.example:443?encryption=none#%F0%9F%87%B8%F0%9F%87%AA%20CDN",
        "vless://disc-xhttp@discovery.example:443?encryption=none#%F0%9F%87%B8%F0%9F%87%AA%20XHTTP",
        "vless://disc-direct@discovery.example:443?encryption=none#%F0%9F%87%B8%F0%9F%87%AA%20DIRECT",
    ]
    user1_uuid_cdn = str(uuid.uuid4())
    user1_uuid_xhttp = str(uuid.uuid4())
    user2_uuid_cdn = str(uuid.uuid4())
    user2_uuid_xhttp = str(uuid.uuid4())

    async with GetDB() as db:
        tenant = Tenant(name=f"uuid_{uuid.uuid4().hex[:10]}", status=TenantStatus.active)
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
        dest_a, dest_b, dest_c = dest_rows[0], dest_rows[1], dest_rows[2]
        for row in dest_rows:
            row.virtual_inbound_tag = None
            payload = dict(row.source_payload or {})
            payload.pop("oc_source_config_id", None)
            row.source_payload = payload

        tag_a = destination_virtual_inbound_tag(panel.id, dest_a.destination_config_id)
        tag_b = destination_virtual_inbound_tag(panel.id, dest_b.destination_config_id)
        tag_c = destination_virtual_inbound_tag(panel.id, dest_c.destination_config_id)

        upstream_by_user: dict[int, list[str]] = {}
        user_specs = [
            ("one", user1_uuid_cdn, user1_uuid_xhttp, "https://1.1.1.1/sub/user-one"),
            ("two", user2_uuid_cdn, user2_uuid_xhttp, "https://1.1.1.1/sub/user-two"),
        ]
        user_ids: list[int] = []
        for suffix, u_cdn, u_xhttp, sub_url in user_specs:
            user = User(
                username=f"u_{suffix}_{uuid.uuid4().hex[:6]}",
                status=UserStatus.active,
                admin_id=admin.id,
                workspace_id=workspace_id,
                proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            )
            db.add(user)
            await db.flush()
            upstream_by_user[user.id] = [
                _vless(u_cdn, "customer.example", 8443, dest_a.display_name),
                _vless(u_xhttp, "customer.example", 8444, dest_b.display_name),
                _vless(str(uuid.uuid4()), "customer.example", 8445, dest_c.display_name),
            ]
            db.add(
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel.id,
                    external_user_id=f"ext_{suffix}",
                    status="active",
                    last_synced_configs=[],
                    external_subscription_url=sub_url,
                )
            )
            user_ids.append(user.id)
        tenant_id = tenant.id
        await db.commit()

    async def _fetch_body(url: str, fmt: str) -> str:
        if "user-one" in url:
            return "\n".join(upstream_by_user[user_ids[0]])
        if "user-two" in url:
            return "\n".join(upstream_by_user[user_ids[1]])
        return ""

    conn_patch = patch(
        "app.services.oc_subscription_runtime.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    )
    with conn_patch, patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(side_effect=_fetch_body),
    ):
        for uid in user_ids:
            async with GetDB() as db:
                finals = await collect_oc_subscription_links_for_user(
                    db,
                    uid,
                    [tag_a, tag_b],
                    {},
                    user_tenant_id=tenant_id,
                )
            assert len(finals) == 2
            upstream = upstream_by_user[uid]
            upstream_bases = {share_link_config_base(u) for u in upstream}
            upstream_uuids = {vless_trojan_client_uuid(u) for u in upstream if vless_trojan_client_uuid(u)}

            for final in finals:
                base = share_link_config_base(final)
                assert base in upstream_bases
                final_uuid = vless_trojan_client_uuid(final)
                assert final_uuid is not None
                assert final_uuid in upstream_uuids
                assert "disc-cdn" not in base
                assert "disc-xhttp" not in base
                assert "discovery.example" not in base

            other_uid = user_ids[1] if uid == user_ids[0] else user_ids[0]
            other_uuids = {
                vless_trojan_client_uuid(u)
                for u in upstream_by_user[other_uid]
                if vless_trojan_client_uuid(u)
            }
            for final in finals:
                assert vless_trojan_client_uuid(final) not in other_uuids

        async with GetDB() as db:
            only_c = await collect_oc_subscription_links_for_user(
                db, user_ids[0], [tag_c], {}, user_tenant_id=tenant_id
            )
        assert len(only_c) == 1
        assert share_link_config_base(only_c[0]) == share_link_config_base(upstream_by_user[user_ids[0]][2])


async def main():
    await test_upstream_uuid_and_config_base_preserved_two_users()
    print("test_oc_subscription_uuid_preservation: OK")


if __name__ == "__main__":
    asyncio.run(main())
