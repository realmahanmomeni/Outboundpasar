#!/usr/bin/env python3
"""Dry-run repair for Groups with zero inbound associations (audit / optional apply).

Usage:
  python scripts/repair_empty_group_associations.py --group-ids 2568,2569,2570,2571,2572
  python scripts/repair_empty_group_associations.py --group-ids 2572 --apply  # requires explicit flag

Does NOT run against production unless --apply is passed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.db import GetDB
from app.db.models import Group, User, inbounds_groups_association, users_groups_association  # noqa: PLC0415
from app.db.models_oc import OCUserMapping


async def _investigate_group(db, group: Group) -> list[str]:
    """Return human-readable evidence lines; empty list means no deterministic rebuild path."""
    notes: list[str] = []
    user_count = await db.scalar(
        select(func.count())
        .select_from(users_groups_association)
        .where(users_groups_association.c.groups_id == group.id)
    )
    notes.append(f"users_in_group={int(user_count or 0)}")
    if user_count:
        user_ids = (
            await db.scalars(
                select(users_groups_association.c.user_id).where(
                    users_groups_association.c.groups_id == group.id
                )
            )
        ).all()
        mappings = (
            await db.execute(
                select(OCUserMapping.panel_id, OCUserMapping.last_synced_configs).where(
                    OCUserMapping.user_id.in_(user_ids)
                )
            )
        ).all()
        notes.append(f"oc_user_mappings={len(mappings)}")
        if mappings:
            notes.append(
                "last_synced_configs describe per-user OC desired state, not historical group inbound_tags"
            )
    notes.append("no persisted inbound_tags snapshot or association audit table found")
    return notes


async def _run(group_ids: list[int], apply: bool) -> int:
    inspected = repaired = skipped = added = 0
    async with GetDB() as db:
        for gid in group_ids:
            inspected += 1
            group = await db.get(Group, gid)
            if group is None:
                print(f"group {gid}: NOT FOUND")
                skipped += 1
                continue
            count = await db.scalar(
                select(func.count())
                .select_from(inbounds_groups_association)
                .where(inbounds_groups_association.c.group_id == gid)
            )
            count = int(count or 0)
            for line in await _investigate_group(db, group):
                print(f"  {line}")
            print(
                f"group {gid} tenant={group.tenant_id} name={group.name!r} "
                f"associations={count} proposed=SKIP reason=requires_manual_operator_action"
            )
            skipped += 1
            if apply and count == 0:
                print(
                    "  --apply: refused — no deterministic reconstruction source "
                    "(needs historical inbound_tags or operator-confirmed tag list)"
                )
    print(
        f"summary inspected={inspected} repaired={repaired} skipped={skipped} "
        f"associations_added={added} associations_unchanged={inspected}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect/repair empty group inbound associations")
    parser.add_argument("--group-ids", required=True, help="Comma-separated group ids")
    parser.add_argument("--apply", action="store_true", help="Apply repairs (default: dry-run only)")
    args = parser.parse_args()
    ids = [int(x.strip()) for x in args.group_ids.split(",") if x.strip()]
    if args.apply:
        print("WARNING: --apply requested but reconstruction requires manual audit data.", file=sys.stderr)
    return asyncio.run(_run(ids, apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
