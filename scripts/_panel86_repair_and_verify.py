"""Ops: restore group Panel 86 destinations, run reconcile/sync, verify users 3138/3139."""
from __future__ import annotations

import asyncio
import base64
import re
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.crud.group import modify_group
from app.db.crud.user import get_users_by_ids
from app.db.models import Group, User
from app.db.models_oc import OCPanel, OCUserMapping, OCSyncState
from app.jobs.process_oc_sync import process_oc_sync
from app.jobs.reconcile_oc_sync import reconcile_all_oc_users
from app.models.settings import ConfigFormat
from app.models.group import GroupModify
from app.node.oc_sync import enqueue_oc_user_sync_many, resolve_desired_config_ids_for_panel
from app.services.oc_inbound_tags import validate_inbound_tags_for_group
from app.operation.subscription import SubscriptionOperation
from app.services.oc_panel_destination_hosts import ensure_panel_destination_hosts_materialized
from app.services.oc_reconcile import classify_mapping_action, mapping_is_healthy, DesiredPanelState, normalize_config_set
from app.services.oc_share_link import parse_destination_tag

PANEL_ID = 2656
GROUP_ID = 1406
USER_IDS = (3138, 3139)
ADMIN_ID = 243
OC_TAGS = [
    "Shadowsocks TCP",
    "oc_2656_d_67eda724a41c2ae6",
    "oc_2656_d_c5e0794d4573ee58",
    "oc_2656_d_d7ad92194db8ebeb",
]
EXPECTED_SUB_PATHS = {
    3138: "259eb6e0870d60ef51779bcbe4af4d521",
    3139: "88591046b3961d0f1c4dadb90f9646d",
}


def _uuid_from_link(link: str) -> str | None:
    base = link.split("#", 1)[0]
    m = re.search(r"://([^@]+)@", base)
    return m.group(1) if m else None


async def main() -> None:
    async with GetDB() as db:
        panel = await db.get(OCPanel, PANEL_ID)
        n = await ensure_panel_destination_hosts_materialized(db, panel)
        await db.commit()
        print(f"materialized_destination_hosts={n}")

        g = (
            await db.execute(
                select(Group).options(selectinload(Group.inbounds)).where(Group.id == GROUP_ID)
            )
        ).scalar_one()
        before = [ib.tag for ib in g.inbounds]
        print(f"group {GROUP_ID} before_tags={before}")

        if set(OC_TAGS) - set(before):

            async def _raise(msg: str, code: int) -> None:
                raise RuntimeError(f"{code}: {msg}")

            await validate_inbound_tags_for_group(
                db,
                OC_TAGS,
                tenant_id=g.tenant_id,
                workspace_id=g.workspace_id,
                raise_error=_raise,
            )
            await modify_group(
                db,
                g,
                GroupModify(name=g.name, inbound_tags=OC_TAGS, is_disabled=g.is_disabled),
            )
            users = await get_users_by_ids(db, USER_IDS)
            await enqueue_oc_user_sync_many(db, users)
            await db.commit()
            print(f"group updated inbound_tags={OC_TAGS} oc_sync_enqueued_users={len(users)}")
        else:
            print("group already has panel 86 destination tags")

    for round_i in range(12):
        print(f"--- sync round {round_i + 1} ---")
        await reconcile_all_oc_users()
        for _ in range(8):
            await process_oc_sync()

        async with GetDB() as db:
            pending = (
                await db.execute(
                    select(OCSyncState.id).where(
                        OCSyncState.status == "pending",
                        OCSyncState.entity_id.in_(tuple(f"{u}_{PANEL_ID}" for u in USER_IDS)),
                    )
                )
            ).scalars().all()
            print(f"pending_jobs_for_users={len(pending)}")

        all_ok = True
        async with GetDB() as db:
            for uid in USER_IDS:
                user = (
                    await db.execute(
                        select(User)
                        .options(selectinload(User.groups).selectinload(Group.inbounds))
                        .where(User.id == uid)
                    )
                ).scalar_one()
                tags = []
                for gr in user.groups:
                    for ib in gr.inbounds:
                        tags.append(ib.tag)
                oc_tags = [t for t in tags if parse_destination_tag(t)]
                desired = await resolve_desired_config_ids_for_panel(db, PANEL_ID, oc_tags)
                mapping = (
                    await db.execute(
                        select(OCUserMapping).where(
                            OCUserMapping.user_id == uid, OCUserMapping.panel_id == PANEL_ID
                        )
                    )
                ).scalar_one_or_none()
                if not mapping:
                    print(f"user {uid}: NO mapping")
                    all_ok = False
                    continue
                dset = normalize_config_set(desired)
                action = classify_mapping_action(
                    mapping, DesiredPanelState(panel_id=PANEL_ID, desired_active=bool(dset), config_ids=dset)
                )
                healthy = mapping_is_healthy(mapping, dset) if dset else False
                print(
                    f"user {uid}: status={mapping.status} desired={sorted(dset)} "
                    f"action={action} healthy={healthy} url={(mapping.external_subscription_url or '')[:80]}"
                )
                if not healthy:
                    all_ok = False
        if all_ok and not pending:
            break
        await asyncio.sleep(15)

    sub_op = SubscriptionOperation()
    async with GetDB() as db:
        for uid in USER_IDS:
            user = await db.get(User, uid)
            resp = await sub_op.user_subscription_by_user(db, user, ConfigFormat.links)
            body = resp.body if hasattr(resp, "body") else b""
            if isinstance(body, bytes):
                text = body.decode(errors="replace")
            else:
                text = str(body)
            try:
                decoded = base64.b64decode(text.strip()).decode(errors="replace")
            except Exception:
                decoded = text
            lines = [ln for ln in decoded.splitlines() if ln.strip()]
            panel86_lines = [ln for ln in lines if "oc_2656" in ln or any(t in ln for t in OC_TAGS[1:])]
            uuids = [_uuid_from_link(ln) for ln in panel86_lines]
            print(f"\n=== USER {uid} pasarguard subscription ===")
            print(f"total_links={len(lines)} panel86_related={len(panel86_lines)}")
            for ln in panel86_lines[:6]:
                print(f"  {_uuid_from_link(ln)!r} remark={ln.split('#')[-1] if '#' in ln else ''}")

            ext_url = (
                await db.execute(
                    select(OCUserMapping.external_subscription_url).where(
                        OCUserMapping.user_id == uid, OCUserMapping.panel_id == PANEL_ID
                    )
                )
            ).scalar_one_or_none()
            if ext_url:
                path = urlparse(ext_url).path
                print(f"external_subscription_url path={path}")
                expected = EXPECTED_SUB_PATHS[uid]
                print(f"expected_sub_token_match={expected in (path or '')}")


if __name__ == "__main__":
    asyncio.run(main())
