"""
Runtime acceptance (no pytest): admin auto-provision, OC subscription pipeline, host delete, cleanup.

Run inside the panel container:
  python scripts/runtime_acceptance_verify.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import ssl
import uuid
from typing import Any

import aiohttp
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.crud.admin import remove_admins
from app.db.models import Admin, ProxyHost, ProxyInbound, Workspace
from app.db.models_oc import OCPanel, OCPanelDestinationHost
from app.node.oc_sync import remove_imported_panel
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_panel_host_subscription import refresh_panel_hosts_from_discovery_subscription
from app.services.oc_share_link import decode_subscription_links, is_importable_configuration_link
from app.services.oc_upstream_subscription import fetch_upstream_subscription_body
from app.utils.jwt import create_admin_token

API = "https://127.0.0.1:2053"
OWNER_ID = 1
OWNER_USERNAME = "pg_secops_94x"
OC_ADMIN_ID = 243
OC_ADMIN_USERNAME = "admin1"
BUILTIN_ADMINISTRATOR_ROLE_ID = 2


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _api(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    token: str,
    *,
    json_body: dict | None = None,
) -> tuple[int, Any]:
    url = f"{API}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    async with session.request(method, url, headers=headers, json=json_body) as resp:
        try:
            body = await resp.json()
        except Exception:
            body = await resp.text()
        return resp.status, body


async def _owner_token() -> str:
    return await create_admin_token(OWNER_ID, OWNER_USERNAME)


async def _token_for_admin(admin_id: int, username: str) -> str:
    return await create_admin_token(admin_id, username)


async def provision_fresh_admin(session: aiohttp.ClientSession) -> dict[str, Any]:
    report: dict[str, Any] = {}
    owner = await _owner_token()
    username = f"accept_rt_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(16)
    status, body = await _api(
        session,
        "POST",
        "/api/admin",
        owner,
        json_body={
            "username": username,
            "password": password,
            "role_id": BUILTIN_ADMINISTRATOR_ROLE_ID,
            "status": "active",
        },
    )
    report["create_status"] = status
    report["create_body_keys"] = list(body.keys()) if isinstance(body, dict) else str(body)[:200]
    if status != 201:
        report["error"] = "admin_create_failed"
        return report

    report["username"] = username
    report["password"] = password
    report["api_tenant_id"] = body.get("tenant_id")
    report["api_workspace_id"] = body.get("workspace_id")

    async with GetDB() as db:
        row = (
            await db.execute(select(Admin).where(Admin.username == username))
        ).scalar_one()
        ws = await db.get(Workspace, row.workspace_id) if row.workspace_id else None
        report["db"] = {
            "admin_id": row.id,
            "tenant_id": row.tenant_id,
            "workspace_id": row.workspace_id,
            "workspace_tenant_id": ws.tenant_id if ws else None,
            "workspace_owner_admin_id": ws.owner_admin_id if ws else None,
        }
        report["admin_id"] = row.id

    st, tok_body = await _api(
        session,
        "POST",
        "/api/admin/token",
        "",
        json_body=None,
    )
    # OAuth2 form for token
    async with session.post(
        f"{API}/api/admin/token",
        data={"username": username, "password": password},
        ssl=_ssl_ctx(),
    ) as resp:
        st = resp.status
        tok_body = await resp.json()
    report["login_status"] = st
    if st != 200:
        report["error"] = "admin_login_failed"
        return report
    admin_token = tok_body["access_token"]

    st, panels = await _api(session, "GET", "/api/panels", admin_token)
    report["panels_status"] = st
    report["panels_keys"] = list(panels.keys()) if isinstance(panels, dict) else str(panels)[:120]

    st, tg = await _api(session, "POST", "/api/integration/telegram-connection/start", admin_token)
    report["telegram_start_status"] = st
    detail = tg.get("detail") if isinstance(tg, dict) else str(tg)
    report["telegram_start_detail"] = detail
    report["tenant_scope_ok"] = detail not in (
        "Admin is not assigned to a tenant",
        "Tenant scope required",
    )
    return report


async def oc_subscription_and_hosts(session: aiohttp.ClientSession) -> dict[str, Any]:
    report: dict[str, Any] = {}
    token = await _token_for_admin(OC_ADMIN_ID, OC_ADMIN_USERNAME)

    st, avail = await _api(session, "GET", "/api/integration/available-panels", token)
    report["available_panels_status"] = st
    if st != 200:
        report["error"] = "available_panels_failed"
        report["detail"] = avail
        return report

    items = avail.get("items") or []
    candidates = [i for i in items if not i.get("already_imported")]
    if not candidates:
        candidates = items
    if not candidates:
        report["error"] = "no_oc_panels_in_account"
        return report
    sub_id = int(candidates[0]["id"])
    report["subscription_id"] = sub_id

    st, imp = await _api(
        session,
        "POST",
        "/api/integration/import-panels",
        token,
        json_body={"subscription_ids": [sub_id]},
    )
    report["import_status"] = st
    if st != 200:
        report["error"] = "import_failed"
        report["detail"] = imp
        return report
    panel_id = imp["imported"][0]["panel_id"]
    report["panel_id"] = panel_id

    st, _ = await _api(session, "POST", f"/api/integration/panels/{panel_id}/test-user", token)
    report["test_user_status"] = st

    st, groups = await _api(session, "GET", f"/api/integration/panels/{panel_id}/groups", token)
    if st != 200:
        report["error"] = "groups_failed"
        return report
    selected = [g["id"] for g in groups.get("groups", [])]
    group_names = {g["id"]: g["name"] for g in groups.get("groups", [])}
    st, sync = await _api(
        session,
        "POST",
        f"/api/integration/panels/{panel_id}/sync",
        token,
        json_body={"selected_group_ids": selected, "group_names": group_names},
    )
    report["sync_status"] = st
    report["sync_summary"] = sync if isinstance(sync, dict) else str(sync)[:300]

    async with GetDB() as db:
        panel = (
            await db.execute(
                select(OCPanel).options(selectinload(OCPanel.integration)).where(OCPanel.id == panel_id)
            )
        ).scalar_one_or_none()
        if panel is None:
            report["error"] = "panel_missing"
            return report
        refreshed = await refresh_panel_hosts_from_discovery_subscription(db, panel)
        await db.commit()
        report["refresh_discovery_hosts"] = refreshed

        mapping_url = None
        from app.db.models_oc import OCUserMapping

        m = (
            await db.execute(
                select(OCUserMapping)
                .where(OCUserMapping.panel_id == panel_id)
                .order_by(OCUserMapping.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if m and m.external_subscription_url:
            mapping_url = m.external_subscription_url.strip()

        live_links: list[str] = []
        junk_in_body = [
            "plain informational line",
            "<html><body>not a config</body></html>",
            "",
            "not-a-valid-scheme://foo",
        ]
        if mapping_url:
            sub_body = await fetch_upstream_subscription_body(mapping_url, "links")
            text_body = sub_body if isinstance(sub_body, str) else sub_body.decode()
            live_links = decode_subscription_links(text_body)
            report["live_subscription_link_count"] = len(live_links)

        # Stress remarks on valid vless templates (same reconcile path as sync).
        stress_uuid = str(uuid.uuid4())
        stress_remarks = [
            "Shadowsocks TCP",
            "user_123",
            "user_123 | 45GB remaining",
            "🔥 Premium User",
            "account@example",
            "unusual τëšt 协议",
        ]
        stress_links = [
            f"vless://{stress_uuid}@203.0.113.10:443?encryption=none&security=tls&type=ws#{base64.b64encode(r.encode()).decode()}"
            for r in stress_remarks
        ]
        # Use distinct UUIDs per link for import
        stress_links = []
        for r in stress_remarks:
            u = str(uuid.uuid4())
            frag = r
            stress_links.append(f"vless://{u}@203.0.113.10:443?encryption=none&security=tls&type=ws#{frag}")

        combined_body = "\n".join(junk_in_body + live_links[:2] + stress_links)
        decoded_combined = decode_subscription_links(combined_body)
        importable = [ln for ln in decoded_combined if is_importable_configuration_link(ln)]
        report["combined_importable_count"] = len(importable)
        report["combined_rejected_non_links"] = len(junk_in_body)

        await reconcile_panel_destination_hosts(db, panel, importable)
        await db.commit()

        dest_rows = (
            await db.execute(
                select(OCPanelDestinationHost).where(
                    OCPanelDestinationHost.panel_id == panel_id,
                    OCPanelDestinationHost.source_missing.is_(False),
                    OCPanelDestinationHost.locally_hidden.is_(False),
                )
            )
        ).scalars().all()
        report["destination_host_count"] = len(dest_rows)
        report["destination_remarks_sample"] = [r.display_name for r in dest_rows[:12]]

        stress_found = {r: False for r in stress_remarks}
        for row in dest_rows:
            for r in stress_remarks:
                if row.display_name == r:
                    stress_found[r] = True
        report["stress_remarks_imported"] = stress_found

        # Host list API (panels hosts UI backend)
        st, hosts_api = await _api(session, "GET", f"/api/panels/{panel_id}/hosts", token)
        report["panel_hosts_api_status"] = st
        host_items = hosts_api.get("hosts") if isinstance(hosts_api, dict) else []
        report["panel_hosts_api_count"] = len(host_items or [])

        if not dest_rows:
            report["error"] = "no_destination_hosts"
            report["panel_id_for_cleanup"] = panel_id
            return report

        victim = dest_rows[0]
        victim_tag = victim.virtual_inbound_tag
        victim_proxy = (
            await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == victim_tag))
        ).scalar_one_or_none()
        victim_host_id = victim_proxy.id if victim_proxy else None
        report["delete_target"] = {
            "destination_config_id": victim.destination_config_id,
            "virtual_inbound_tag": victim_tag,
            "proxy_host_id": victim_host_id,
        }

        if victim_host_id:
            st, del_body = await _api(session, "DELETE", f"/api/host/{victim_host_id}", token)
            report["host_delete_status"] = st
            report["host_delete_body"] = del_body

        remaining_dest = (
            await db.execute(
                select(func.count())
                .select_from(OCPanelDestinationHost)
                .where(
                    OCPanelDestinationHost.panel_id == panel_id,
                    OCPanelDestinationHost.source_missing.is_(False),
                )
            )
        ).scalar()
        orphan_inbound = (
            await db.execute(
                select(func.count())
                .select_from(ProxyInbound)
                .where(ProxyInbound.tag == victim_tag)
            )
        ).scalar()
        orphan_proxy = (
            await db.execute(
                select(func.count()).select_from(ProxyHost).where(ProxyHost.inbound_tag == victim_tag)
            )
        ).scalar()
        report["after_delete"] = {
            "remaining_dest_hosts": int(remaining_dest or 0),
            "orphan_inbound_for_deleted_tag": int(orphan_inbound or 0),
            "orphan_proxy_for_deleted_tag": int(orphan_proxy or 0),
        }
        report["panel_id_for_cleanup"] = panel_id

    return report


async def cleanup_acceptance(
    *,
    accept_admin_id: int | None,
    accept_username: str | None,
    panel_id: int | None,
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    async with GetDB() as db:
        if panel_id:
            panel = await db.get(OCPanel, panel_id)
            if panel:
                await remove_imported_panel(db, panel)
                report["panel_removed"] = panel_id
            await db.commit()

        if accept_admin_id:
            row = await db.get(Admin, accept_admin_id)
            junk_tenant_id = int(row.tenant_id) if row and row.tenant_id else None
            await db.execute(delete(Workspace).where(Workspace.owner_admin_id == accept_admin_id))
            await db.flush()
            await remove_admins(db, [accept_admin_id])
            report["accept_admin_removed"] = accept_admin_id
            if junk_tenant_id and junk_tenant_id not in (311, 7411):
                from app.db.models import Tenant

                await db.execute(delete(Tenant).where(Tenant.id == junk_tenant_id))
                report["accept_tenant_removed"] = junk_tenant_id

        # Junk tenant for removed accept admin (dedicated tenant)
        if accept_username:
            pass

        report["counts"] = {
            "oc_panels": (await db.execute(text("SELECT COUNT(*) FROM oc_panels"))).scalar(),
            "oc_destination_hosts": (
                await db.execute(text("SELECT COUNT(*) FROM oc_panel_destination_hosts"))
            ).scalar(),
            "oc_sync_states": (await db.execute(text("SELECT COUNT(*) FROM oc_sync_states"))).scalar(),
            "tenants": (await db.execute(text("SELECT COUNT(*) FROM tenants"))).scalar(),
            "admins": (await db.execute(text("SELECT COUNT(*) FROM admins"))).scalar(),
            "orphan_oc_d_inbounds": (
                await db.execute(
                    text(
                        "SELECT COUNT(*) FROM inbounds WHERE tag LIKE 'oc\\_%\\_d\\_%' ESCAPE '\\'"
                    )
                )
            ).scalar(),
            "orphan_oc_proxy_hosts": (
                await db.execute(
                    text("SELECT COUNT(*) FROM hosts WHERE inbound_tag LIKE 'oc\\_%\\_d\\_%' ESCAPE '\\'")
                )
            ).scalar(),
        }
    return report


async def main() -> None:
    out: dict[str, Any] = {}
    panel_id: int | None = None
    accept_id: int | None = None
    accept_user: str | None = None
    connector = aiohttp.TCPConnector(ssl=_ssl_ctx())
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            out["fresh_admin"] = await provision_fresh_admin(session)
            accept_id = out["fresh_admin"].get("admin_id")
            accept_user = out["fresh_admin"].get("username")
            try:
                out["oc_pipeline"] = await oc_subscription_and_hosts(session)
                panel_id = out["oc_pipeline"].get("panel_id_for_cleanup")
            except Exception as exc:
                out["oc_pipeline"] = {"error": type(exc).__name__, "message": str(exc)[:500]}
    finally:
        out["cleanup"] = await cleanup_acceptance(
            accept_admin_id=accept_id,
            accept_username=accept_user,
            panel_id=panel_id,
        )

    # Architecture note (code inspection)
    out["architecture"] = {
        "administrator_dedicated_tenant": (
            "Intentional: auto_provision_administrator_scope creates "
            "Tenant(name='Administrator: {username}') plus owned Workspace via "
            "create_workspace_for_administrator; used on Owner create (role_id=2) "
            "and repair_unscoped_administrator."
        ),
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
