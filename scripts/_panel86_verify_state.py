"""Read-only production state for users 3138/3139 and Panel 2656 (temporary ops script)."""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.models import Group, User, users_groups_association
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelDestinationHost, OCUserMapping, OCSyncState
from app.node.oc_sync import resolve_desired_config_ids_for_panel
from app.services.oc_reconcile import classify_mapping_action, mapping_is_healthy


USER_IDS = (3138, 3139)
PANEL_ID = 2656
GROUP_ID = 1406


async def main() -> None:
    async with GetDB() as db:
        panel = await db.get(OCPanel, PANEL_ID)
        print("=== PANEL ===")
        print(
            json.dumps(
                {
                    "id": panel.id if panel else None,
                    "name": panel.name if panel else None,
                    "sync_status": panel.sync_status if panel else None,
                    "source_panel_id": panel.source_panel_id if panel else None,
                    "tenant_id": panel.tenant_id if panel else None,
                },
                indent=2,
            )
        )

        group = (
            await db.execute(
                select(Group).options(selectinload(Group.inbounds)).where(Group.id == GROUP_ID)
            )
        ).scalar_one_or_none()
        if group:
            inbounds = [ib.tag for ib in (group.inbounds or [])]
            oc_tags = [t for t in inbounds if t.startswith(f"oc_{PANEL_ID}_")]
            print("=== GROUP 1406 ===")
            print(f"inbound_count={len(inbounds)} oc_panel_tags={oc_tags}")
            print("all_tags:", inbounds)

        dests = (
            await db.execute(
                select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == PANEL_ID)
            )
        ).scalars().all()
        print(f"=== DEST HOSTS panel {PANEL_ID} count={len(dests)} ===")
        for d in dests[:8]:
            print(
                f"  tag={d.virtual_inbound_tag!r} hidden={d.locally_hidden} missing={d.source_missing} "
                f"cfg={(d.source_payload or {}).get('oc_source_config_id')!r}"
            )

        configs = (
            await db.execute(select(OCPanelConfig).where(OCPanelConfig.panel_id == PANEL_ID))
        ).scalars().all()
        print(f"=== OCPanelConfig count={len(configs)} ===")

        for uid in USER_IDS:
            print(f"\n=== USER {uid} ===")
            user = (
                await db.execute(
                    select(User).options(selectinload(User.groups)).where(User.id == uid)
                )
            ).scalar_one_or_none()
            if not user:
                print("  NOT FOUND")
                continue
            print(f"  username={user.username!r} status={user.status!r}")
            gids = [g.id for g in user.groups]
            print(f"  groups={gids}")
            user_ib = []
            for g in user.groups:
                for ib in g.inbounds or []:
                    user_ib.append(ib.tag)
            oc_user_tags = [t for t in user_ib if t.startswith(f"oc_{PANEL_ID}_")]
            print(f"  effective_inbound_tags oc_2656={oc_user_tags}")

            mapping = (
                await db.execute(
                    select(OCUserMapping).where(
                        OCUserMapping.user_id == uid, OCUserMapping.panel_id == PANEL_ID
                    )
                )
            ).scalar_one_or_none()
            if mapping:
                desired = await resolve_desired_config_ids_for_panel(db, user, PANEL_ID)
                action = classify_mapping_action(mapping, desired_config_ids=desired)
                healthy = mapping_is_healthy(mapping, desired_config_ids=desired)
                print(
                    json.dumps(
                        {
                            "mapping_status": mapping.status,
                            "external_user_id": mapping.external_user_id,
                            "external_subscription_url": (mapping.external_subscription_url or "")[:120],
                            "last_synced_configs": mapping.last_synced_configs,
                            "desired_config_ids": sorted(desired),
                            "classify_action": str(action),
                            "mapping_is_healthy": healthy,
                        },
                        indent=2,
                    )
                )
            else:
                print("  NO OCUserMapping for panel 2656")

            jobs = (
                await db.execute(
                    select(OCSyncState)
                    .where(OCSyncState.entity_id == f"{uid}_{PANEL_ID}")
                    .order_by(OCSyncState.id.desc())
                    .limit(5)
                )
            ).scalars().all()
            print(f"  recent OCSyncState jobs={len(jobs)}")
            for j in jobs:
                print(
                    f"    id={j.id} op={j.operation} status={j.status} attempts={j.attempts} "
                    f"err={(j.last_error or '')[:80]!r}"
                )


if __name__ == "__main__":
    asyncio.run(main())
