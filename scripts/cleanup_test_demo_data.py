"""
One-off cleanup of test/demo data left by integration test runs.
Preserves: admin pg_secops_94x (id=1), tenant 311, OC integration id=1, telegram connection 119.
"""
from __future__ import annotations

import asyncio
import os
import sys

# Project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PRESERVE_ADMIN_ID = 1
PRESERVE_TENANT_ID = 311
PRESERVE_INTEGRATION_ID = 1
PRESERVE_TELEGRAM_CONNECTION_IDS = {119}


async def run(*, dry_run: bool = False) -> dict:
    from sqlalchemy import delete, select, text
    from sqlalchemy.orm import selectinload

    from app.db import GetDB
    from app.db.crud.admin import remove_admins
    from app.db.crud.group import remove_groups
    from app.db.crud.host import remove_hosts
    from app.db.crud.user import remove_users
    from app.db.models import Admin, Group, ProxyHost, Tenant, User
    from app.db.models_oc import OCSyncState, UserPanelBinding
    from app.db.models_oc import OCIntegration, OCPanel, TenantTelegramConnection
    from app.node.oc_sync import remove_imported_panel

    report: dict = {
        "dry_run": dry_run,
        "deleted_panels": [],
        "deleted_admins": [],
        "deleted_users": [],
        "deleted_telegram_connections": [],
        "deleted_tenants": [],
        "deleted_integrations": [],
        "preserved": {
            "admin_id": PRESERVE_ADMIN_ID,
            "tenant_id": PRESERVE_TENANT_ID,
            "integration_id": PRESERVE_INTEGRATION_ID,
            "telegram_connection_ids": sorted(PRESERVE_TELEGRAM_CONNECTION_IDS),
        },
    }

    async with GetDB() as db:
        panels = (await db.execute(select(OCPanel).order_by(OCPanel.id))).scalars().all()
        for panel in panels:
            report["deleted_panels"].append(
                {
                    "id": panel.id,
                    "name": panel.name,
                    "source_panel_id": panel.source_panel_id,
                    "integration_id": panel.integration_id,
                    "purchaser_identity": panel.purchaser_identity,
                }
            )
            if not dry_run:
                await remove_imported_panel(db, panel)
        if not dry_run and panels:
            await db.commit()

        tg_rows = (
            await db.execute(select(TenantTelegramConnection).order_by(TenantTelegramConnection.id))
        ).scalars().all()
        for conn in tg_rows:
            if conn.id in PRESERVE_TELEGRAM_CONNECTION_IDS:
                continue
            report["deleted_telegram_connections"].append(
                {"id": conn.id, "tenant_id": conn.tenant_id, "telegram_user_id": conn.telegram_user_id, "status": conn.status}
            )
            if not dry_run:
                await db.delete(conn)
        if not dry_run:
            await db.commit()

        users = (await db.execute(select(User))).scalars().all()
        test_users = [u for u in users if u.admin_id != PRESERVE_ADMIN_ID or _is_test_user_on_secops(u.username)]
        # All users on non-secops admins are test fixture users; all secops users match test patterns.
        for u in test_users:
            report["deleted_users"].append({"id": u.id, "username": u.username, "admin_id": u.admin_id})
        if not dry_run and test_users:
            await remove_users(db, test_users)

        admin_ids = [
            row[0]
            for row in (await db.execute(select(Admin.id).where(Admin.id != PRESERVE_ADMIN_ID))).all()
        ]
        admins = (await db.execute(select(Admin).where(Admin.id.in_(admin_ids)))).scalars().all()
        for a in admins:
            report["deleted_admins"].append(
                {"id": a.id, "username": a.username, "role_id": a.role_id, "tenant_id": a.tenant_id}
            )
        if not dry_run and admin_ids:
            await remove_admins(db, admin_ids)

        tenant_ids = [
            row[0]
            for row in (await db.execute(select(Tenant.id).where(Tenant.id != PRESERVE_TENANT_ID))).all()
        ]
        tenants = (await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))).scalars().all()
        for t in tenants:
            report["deleted_tenants"].append({"id": t.id, "name": t.name})

        group_ids = [
            row[0]
            for row in (
                await db.execute(
                    select(Group.id).where(
                        (Group.tenant_id != PRESERVE_TENANT_ID) | (Group.tenant_id.is_(None))
                    )
                )
            ).all()
        ]
        report["deleted_groups"] = group_ids
        if not dry_run and group_ids:
            await remove_groups(db, group_ids)

        host_ids = [
            row[0]
            for row in (
                await db.execute(
                    select(ProxyHost.id).where(
                        (ProxyHost.tenant_id != PRESERVE_TENANT_ID) | (ProxyHost.tenant_id.is_(None))
                    )
                )
            ).all()
        ]
        report["deleted_hosts"] = host_ids
        if not dry_run and host_ids:
            await remove_hosts(db, host_ids)

        if not dry_run:
            await db.execute(
                delete(UserPanelBinding).where(UserPanelBinding.tenant_id != PRESERVE_TENANT_ID)
            )
            await db.execute(delete(OCSyncState))
            await db.commit()

        if not dry_run and tenant_ids:
            await db.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
            await db.commit()

        integration_ids = [
            row[0]
            for row in (
                await db.execute(select(OCIntegration.id).where(OCIntegration.id != PRESERVE_INTEGRATION_ID))
            ).all()
        ]
        integrations = (
            await db.execute(select(OCIntegration).where(OCIntegration.id.in_(integration_ids)))
        ).scalars().all()
        for i in integrations:
            report["deleted_integrations"].append(
                {"id": i.id, "base_url": i.base_url, "token_preview": i.token_preview, "is_active": i.is_active}
            )
        if not dry_run and integration_ids:
            await db.execute(delete(OCIntegration).where(OCIntegration.id.in_(integration_ids)))
            await db.commit()

        # Integrity spot-checks
        checks = {}
        for label, q in [
            ("oc_panels", "SELECT count(*) FROM oc_panels"),
            ("admins", "SELECT count(*) FROM admins"),
            ("admins_preserved_username", "SELECT username FROM admins WHERE id = 1"),
            ("oc_integrations", "SELECT count(*) FROM oc_integrations"),
            ("oc_integration_prod", "SELECT base_url FROM oc_integrations WHERE id = 1"),
            ("telegram_connections", "SELECT id, tenant_id, telegram_user_id, active FROM tenant_telegram_connections ORDER BY id"),
            ("tenants", "SELECT id, name FROM tenants ORDER BY id"),
            ("users", "SELECT count(*) FROM users"),
        ]:
            r = await db.execute(text(q))
            if "count" in q.lower() and "telegram" not in label:
                checks[label] = r.scalar()
            elif label == "admins_preserved_username":
                checks[label] = r.scalar()
            elif label == "oc_integration_prod":
                checks[label] = r.scalar()
            else:
                checks[label] = [dict(row._mapping) for row in r.fetchall()]
        report["post_checks"] = checks

    return report


def _is_test_user_on_secops(username: str) -> bool:
    prefixes = ("test_phase10_", "test_sync", "test_status", "user_", "u_oc_up_", "test_u_")
    return username.startswith(prefixes) or username in {"test_sync", "test_status"}


async def main():
    dry_run = "--dry-run" in sys.argv
    report = await run(dry_run=dry_run)
    print(f"dry_run={dry_run}")
    for key in (
        "deleted_panels",
        "deleted_admins",
        "deleted_users",
        "deleted_telegram_connections",
        "deleted_tenants",
        "deleted_integrations",
    ):
        print(f"\n{key}: {len(report[key])}")
    print("\npost_checks:", report["post_checks"])


if __name__ == "__main__":
    asyncio.run(main())
