"""Panel 86 acceptance: import via integration routers, group edit, sync, verify."""
from __future__ import annotations

import asyncio
import base64
import re
import sys

import aiohttp
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.crud.admin import build_admin_details, load_admin_attrs
from app.db.crud.group import modify_group
from app.db.crud.user import get_users_by_ids
from app.db.models import Admin, Group, User
from app.db.models_oc import OCPanel, OCPanelDestinationHost, OCUserMapping, OCSyncState
from app.jobs.process_oc_sync import process_oc_sync
from app.jobs.reconcile_oc_sync import reconcile_all_oc_users
from app.models.group import GroupModify
from app.models.settings import ConfigFormat
from app.node.oc_sync import enqueue_oc_user_sync_many
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.routers import integration as integ
from app.routers import panel as panel_router
from app.services.assignable_hosts import list_group_host_options
from app.services.oc_inbound_tags import validate_inbound_tags_for_group
from app.services.oc_share_link import parse_destination_tag, vless_trojan_client_uuid

SOURCE_SUBSCRIPTION_ID = 1506
GROUP_ID = 1406
USER_IDS = (3138, 3139)
ADMIN_ID = 243
TENANT_ID = 311


def _uuid_from_link(line: str) -> str | None:
    return vless_trojan_client_uuid(line.split("#", 1)[0] if "#" in line else line)


async def _admin_context(db):
    admin = (await db.execute(select(Admin).where(Admin.id == ADMIN_ID))).scalar_one()
    await load_admin_attrs(admin, load_users=False, load_usage_logs=False, load_role=True)
    details = build_admin_details(admin)
    return (details.username, False, details)


async def import_and_sync_panel() -> int:
    async with GetDB() as db:
        ctx = await _admin_context(db)
        out = await integ.import_panels(
            body=integ.ImportPanelsRequest(subscription_ids=[SOURCE_SUBSCRIPTION_ID]),
            db=db,
            user_context=ctx,
        )
        panel_id = out.imported[0].panel_id
        print(f"import_panels panel_id={panel_id} created={out.imported[0].created}")

        await integ.create_test_user(panel_id=panel_id, db=db, user_context=ctx)
        await db.refresh((await db.get(OCPanel, panel_id)))

        groups_resp = await integ.get_groups(panel_id=panel_id, db=db, user_context=ctx)
        selected = [g.id for g in groups_resp.groups]
        group_names = {g.id: g.name for g in groups_resp.groups}
        print(f"oc_groups selected={selected}")

        sync_out = await integ.sync_configs_and_hosts(
            panel_id=panel_id,
            req=integ.SyncRequest(selected_group_ids=selected, group_names=group_names),
            db=db,
            user_context=ctx,
        )
        print(f"sync_wizard status={sync_out.status} configs={sync_out.configs_created} hosts={sync_out.hosts_created}")

        panel = await db.get(OCPanel, panel_id)
        print(f"panel after sync id={panel.id} name={panel.name} tenant={panel.tenant_id} ws={panel.workspace_id} status={panel.sync_status}")
        dest_n = (
            await db.execute(
                select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel_id)
            )
        ).scalars().all()
        print(f"destination_hosts={len(dest_n)} tags={[d.virtual_inbound_tag for d in dest_n[:5]]}")
        return panel_id


async def assign_group_destinations(panel_id: int) -> list[str]:
    async with GetDB() as db:
        g = (
            await db.execute(select(Group).options(selectinload(Group.inbounds)).where(Group.id == GROUP_ID))
        ).scalar_one()
        existing = [ib.tag for ib in g.inbounds]
        options = await list_group_host_options(db, tenant_id=TENANT_ID, workspace_id=g.workspace_id)
        oc_tags = sorted({o.inbound_tag for o in options if o.panel_id == panel_id and o.is_oc_destination})
        native = [t for t in existing if not parse_destination_tag(t)]
        inbound_tags = native + oc_tags
        print(f"group {GROUP_ID} assigning tags native={native} oc={oc_tags}")

        async def _raise(msg: str, code: int) -> None:
            raise RuntimeError(f"{code}: {msg}")

        await validate_inbound_tags_for_group(
            db,
            inbound_tags,
            tenant_id=g.tenant_id,
            workspace_id=g.workspace_id,
            raise_error=_raise,
        )
        await modify_group(
            db,
            g,
            GroupModify(name=g.name, inbound_tags=inbound_tags, is_disabled=g.is_disabled),
        )
        return inbound_tags


async def sync_users(panel_id: int) -> None:
    for round_i in range(10):
        await reconcile_all_oc_users()
        for _ in range(12):
            await process_oc_sync()
        async with GetDB() as db:
            pending = (
                await db.execute(
                    select(OCSyncState.id).where(
                        OCSyncState.status == "pending",
                        OCSyncState.entity_id.in_(tuple(f"{u}_{panel_id}" for u in USER_IDS)),
                    )
                )
            ).scalars().all()
            ok = True
            for uid in USER_IDS:
                m = (
                    await db.execute(
                        select(OCUserMapping).where(
                            OCUserMapping.user_id == uid, OCUserMapping.panel_id == panel_id
                        )
                    )
                ).scalar_one_or_none()
                if not m or m.status != "active" or not (m.external_subscription_url or "").startswith("http"):
                    ok = False
                print(f"  round {round_i+1} user {uid} mapping={m.status if m else None} url={(m.external_subscription_url or '')[:70] if m else ''}")
            if ok and not pending:
                return
        await asyncio.sleep(8)


async def fetch_text(url: str) -> str:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.get(url) as r:
            raw = await r.text()
    try:
        return base64.b64decode(raw.strip()).decode()
    except Exception:
        return raw


async def verify_subscriptions(panel_id: int) -> bool:
    sub_op = SubscriptionOperation(OperatorType.API)
    upstream_uuids: dict[int, set[str]] = {}
    final_uuids: dict[int, set[str]] = {}
    async with GetDB() as db:
        for uid in USER_IDS:
            m = (
                await db.execute(
                    select(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == panel_id)
                )
            ).scalar_one()
            up_body = await fetch_text(m.external_subscription_url)
            up_lines = [ln for ln in up_body.splitlines() if ln.strip()]
            upstream_uuids[uid] = {u for ln in up_lines for u in [_uuid_from_link(ln)] if u}
            user = await db.get(User, uid)
            resp = await sub_op.user_subscription_by_user(db, user, ConfigFormat.links)
            body = resp.body.decode() if isinstance(resp.body, bytes) else str(resp.body)
            try:
                decoded = base64.b64decode(body.strip()).decode()
            except Exception:
                decoded = body
            fin_lines = [ln for ln in decoded.splitlines() if ln.strip()]
            final_uuids[uid] = {u for ln in fin_lines for u in [_uuid_from_link(ln)] if u}
            print(f"USER {uid} upstream_lines={len(up_lines)} final_lines={len(fin_lines)}")
            print(f"  upstream_uuids={sorted(upstream_uuids[uid])}")
            print(f"  final_uuids={sorted(final_uuids[uid])}")
            for ln in fin_lines:
                if parse_destination_tag(ln) or f"oc_{panel_id}_" in ln:
                    print(f"  oc_line remark={(ln.split('#')[-1] if '#' in ln else '')[:60]}")
    leak = upstream_uuids[3138] & upstream_uuids[3139]
    cross = final_uuids[3138] & final_uuids[3139]
    print(f"cross_upstream_uuid_leak={bool(leak)} cross_final_uuid_leak={bool(cross)}")
    ok3138 = upstream_uuids[3138].issubset(final_uuids[3138]) or bool(upstream_uuids[3138] & final_uuids[3138])
    ok3139 = upstream_uuids[3139].issubset(final_uuids[3139]) or bool(upstream_uuids[3139] & final_uuids[3139])
    return ok3138 and ok3139 and not cross


async def main() -> int:
    panel_id = await import_and_sync_panel()
    await assign_group_destinations(panel_id)
    async with GetDB() as db:
        users = await get_users_by_ids(db, USER_IDS)
        for u in users:
            u = (
                await db.execute(
                    select(User).options(selectinload(User.groups).selectinload(Group.inbounds)).where(User.id == u.id)
                )
            ).scalar_one()
        await enqueue_oc_user_sync_many(db, users)
        await db.commit()
    await sync_users(panel_id)
    ok = await verify_subscriptions(panel_id)
    print(f"ACCEPTANCE_SUB_OK={ok} panel_id={panel_id}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
