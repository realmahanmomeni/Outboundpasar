"""
Remove all OC panel imports and provisioning junk tenants; preserve core admins.

Preserves:
  - Owner (admin id=1)
  - Production administrators: admin1 (243), eversong (12130) unless --no-preserve-eversong
  - Tenant 311 (admin1 org) and tenants assigned to preserved administrators

Uses ``remove_imported_panel`` for each OCPanel row (group refs, ProxyHost, mappings).

Run:
  python scripts/cleanup_all_oc_environment.py --dry-run
  python scripts/cleanup_all_oc_environment.py
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PRESERVE_ADMIN_IDS = (1, 243, 12130)
PRESERVE_TENANT_ID_FALLBACK = 311
TENANT_DELETE_BATCH = 400


async def _purge_junk_admins(db, report: dict) -> None:
    """Remove administrators outside the preserve list (test/scoped leftovers)."""
    from sqlalchemy import delete, select

    from app.db.crud.admin import remove_admins
    from app.db.models import Admin, Workspace

    junk_ids = [
        int(aid)
        for aid in (
            await db.execute(select(Admin.id).where(Admin.id.not_in(PRESERVE_ADMIN_IDS)))
        ).scalars().all()
    ]
    if not junk_ids:
        return
    await db.execute(delete(Workspace).where(Workspace.owner_admin_id.in_(junk_ids)))
    await db.flush()
    await remove_admins(db, junk_ids)
    report["junk_admins_removed"] = len(junk_ids)


async def _delete_junk_tenant_rows(db, junk_tenant_ids: list[int]) -> None:
    """Delete tenant-scoped rows that block ``DELETE FROM tenants``."""
    from sqlalchemy import delete, select

    from app.db.crud.group import remove_groups
    from app.db.models import ClientTemplate, Group, ProxyHost, Settings, Tenant, UserTemplate, Workspace
    from app.db.models_oc import TenantTelegramConnection

    for offset in range(0, len(junk_tenant_ids), TENANT_DELETE_BATCH):
        batch = junk_tenant_ids[offset : offset + TENANT_DELETE_BATCH]
        await db.execute(delete(ProxyHost).where(ProxyHost.tenant_id.in_(batch)))
        await db.execute(delete(Settings).where(Settings.tenant_id.in_(batch)))
        await db.execute(delete(ClientTemplate).where(ClientTemplate.tenant_id.in_(batch)))
        await db.execute(delete(UserTemplate).where(UserTemplate.tenant_id.in_(batch)))
        group_ids = [
            int(gid)
            for gid in (
                await db.execute(select(Group.id).where(Group.tenant_id.in_(batch)))
            ).scalars().all()
        ]
        if group_ids:
            await remove_groups(db, group_ids)
        await db.execute(
            delete(TenantTelegramConnection).where(TenantTelegramConnection.tenant_id.in_(batch))
        )
        await db.execute(delete(Workspace).where(Workspace.tenant_id.in_(batch)))
        await db.execute(delete(Tenant).where(Tenant.id.in_(batch)))
        await db.flush()


async def run(dry_run: bool, repair_admins: bool) -> dict:
    from sqlalchemy import delete, select, text

    from app.db import GetDB
    from app.db.models import Admin, Tenant
    from app.db.models_oc import (
        OCPanel,
        OCSyncState,
        UserPanelBinding,
    )
    from app.node.oc_sync import remove_imported_panel
    from app.services.admin_provision import repair_unscoped_administrator

    report: dict = {"dry_run": dry_run, "panels_removed": 0, "tenants_removed": 0}

    async with GetDB() as db:
        preserved_admins = (
            await db.execute(select(Admin).where(Admin.id.in_(PRESERVE_ADMIN_IDS)))
        ).scalars().all()
        preserve_tenant_ids = {PRESERVE_TENANT_ID_FALLBACK}
        for a in preserved_admins:
            if a.tenant_id is not None:
                preserve_tenant_ids.add(int(a.tenant_id))

        panels = (await db.execute(select(OCPanel).order_by(OCPanel.id))).scalars().all()
        report["panel_ids"] = [p.id for p in panels]
        if not dry_run:
            for panel in panels:
                await remove_imported_panel(db, panel)
                report["panels_removed"] += 1
            await db.commit()

        if not dry_run:
            await db.execute(delete(OCSyncState))
            await db.execute(delete(UserPanelBinding))
            await db.commit()

        if repair_admins and not dry_run:
            for row in preserved_admins:
                await db.refresh(row, attribute_names=["role"])
                if await repair_unscoped_administrator(db, row):
                    report.setdefault("repaired_admins", []).append(row.username)
            await db.commit()
            preserved_admins = (
                await db.execute(select(Admin).where(Admin.id.in_(PRESERVE_ADMIN_IDS)))
            ).scalars().all()
            preserve_tenant_ids = {PRESERVE_TENANT_ID_FALLBACK}
            for a in preserved_admins:
                if a.tenant_id is not None:
                    preserve_tenant_ids.add(int(a.tenant_id))

        if not dry_run:
            await _purge_junk_admins(db, report)
            preserve_tenant_ids = {PRESERVE_TENANT_ID_FALLBACK}
            for a in (
                await db.execute(select(Admin).where(Admin.id.in_(PRESERVE_ADMIN_IDS)))
            ).scalars().all():
                if a.tenant_id is not None:
                    preserve_tenant_ids.add(int(a.tenant_id))

        junk_tenant_ids = [
            int(tid)
            for tid in (
                await db.execute(
                    select(Tenant.id).where(Tenant.id.not_in(preserve_tenant_ids))
                )
            ).scalars().all()
        ]
        report["junk_tenant_candidates"] = len(junk_tenant_ids)

        if not dry_run and junk_tenant_ids:
            await _delete_junk_tenant_rows(db, junk_tenant_ids)
            report["tenants_removed"] = len(junk_tenant_ids)
            await db.commit()

        report["post_checks"] = {
            "oc_panels": (await db.execute(text("SELECT COUNT(*) FROM oc_panels"))).scalar(),
            "oc_destination_hosts": (
                await db.execute(text("SELECT COUNT(*) FROM oc_panel_destination_hosts"))
            ).scalar(),
            "oc_sync_state": (await db.execute(text("SELECT COUNT(*) FROM oc_sync_states"))).scalar(),
            "admins": (await db.execute(text("SELECT id, username, tenant_id, workspace_id FROM admins ORDER BY id"))).all(),
            "tenants": (await db.execute(text("SELECT COUNT(*) FROM tenants"))).scalar(),
        }

    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-repair-admins", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(dry_run=args.dry_run, repair_admins=not args.no_repair_admins))
    print(report)


if __name__ == "__main__":
    main()
