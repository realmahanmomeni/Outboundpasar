"""
Remove Admin/User rows created by pytest/integration tests (username provenance only).

Preserves owner (id=1), production admin1 (id=243), and eversong (id=12130).
Does NOT delete panels, integrations, or tenant 311.

Run:  python scripts/cleanup_verified_test_principals.py --dry-run
      python scripts/cleanup_verified_test_principals.py
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PRESERVE_ADMIN_IDS = (1, 243, 12130)
PRESERVE_TENANT_ID = 311

# Patterns match test_*.py factories (see repo grep: adm_, scoped_, auto_org_, phase workspace a_/b_/op_, etc.)
TEST_ADMIN_SQL = """
    id NOT IN (1, 243, 12130)
    AND (
        username ~ '^adm_[0-9a-f]+$'
        OR username ~ '^scoped_[0-9a-f]+$'
        OR username ~ '^auto_org_[0-9a-f]+$'
        OR username ~ '^orphan_test_'
        OR username ~ '^[abop]_[0-9a-f]{4}$'
        OR username ~ '^adm_[ab]_'
        OR username ~ '^op_[ab]_'
        OR username ~ '^a_[0-9a-f]{8}$'
        OR username ~ '^b_[0-9a-f]{8}$'
        OR username ~ '^tenant-admin'
        OR username ~ '^tenant_admin_'
        OR username LIKE 'ta_admin_%'
        OR username LIKE 'ta_single_%'
        OR username ~ '^u_[0-9a-f]{6}$'
        OR username ~ '^op_[0-9a-f]+$'
        OR username = 'admin1_1'
    )
"""

TEST_USER_SQL = """
    username ~ '^u_[0-9a-f]{6}$'
    OR username ~ '^user_[0-9a-f]{8}$'
    OR username ~ '^ua_[0-9a-f]{6}$'
    OR username ~ '^ub_[0-9a-f]{6}$'
    OR username ~ '^leak_marker_'
    OR username LIKE 'test_phase10_%'
    OR username LIKE 'test_sync%'
    OR username LIKE 'test_status%'
    OR username LIKE 'test_u_%'
    OR username LIKE 'u_oc_up_%'
    OR username ~ '^u_tpl_'
    OR username ~ '^u_c[0-9a-f]{6,}$'
    OR admin_id IN (
        SELECT id FROM admins WHERE """ + TEST_ADMIN_SQL + """
    )
"""

BATCH = 400


async def run(dry_run: bool) -> dict:
    from sqlalchemy import text

    from app.db import GetDB
    from app.db.crud.admin import remove_admins
    from app.db.crud.user import remove_users
    from app.db.models import Admin, User

    report: dict = {"dry_run": dry_run, "users_removed": 0, "admins_removed": 0}

    async with GetDB() as db:
        u_count = (await db.execute(text(f"SELECT COUNT(*) FROM users WHERE {TEST_USER_SQL}"))).scalar() or 0
        a_count = (await db.execute(text(f"SELECT COUNT(*) FROM admins WHERE {TEST_ADMIN_SQL}"))).scalar() or 0
        report["users_matched"] = u_count
        report["admins_matched"] = a_count

        if dry_run:
            report["admins_remaining_est"] = (
                await db.execute(text("SELECT COUNT(*) FROM admins"))
            ).scalar()
            report["users_remaining_est"] = (
                await db.execute(text("SELECT COUNT(*) FROM users"))
            ).scalar()
            return report

        while True:
            ids = (
                await db.execute(
                    text(f"SELECT id FROM users WHERE {TEST_USER_SQL} ORDER BY id LIMIT :lim"),
                    {"lim": BATCH},
                )
            ).scalars().all()
            if not ids:
                break
            from sqlalchemy import select

            rows = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
            await remove_users(db, list(rows))
            report["users_removed"] += len(rows)

        while True:
            from sqlalchemy import select

            ids = (
                await db.execute(
                    text(f"SELECT id FROM admins WHERE {TEST_ADMIN_SQL} ORDER BY id LIMIT :lim"),
                    {"lim": BATCH},
                )
            ).scalars().all()
            if not ids:
                break
            id_list = list(ids)
            await db.execute(
                text("DELETE FROM workspaces WHERE owner_admin_id = ANY(:ids)"),
                {"ids": id_list},
            )
            await db.flush()
            await remove_admins(db, id_list)
            report["admins_removed"] += len(id_list)

        # Orphan workspaces (test administrators)
        await db.execute(
            text(
                """
                DELETE FROM workspaces w
                WHERE w.tenant_id != :tid
                  AND NOT EXISTS (SELECT 1 FROM admins a WHERE a.id = w.owner_admin_id)
                """
            ),
            {"tid": PRESERVE_TENANT_ID},
        )
        # Orphan tenants (no admins, no panels), keep production tenant
        await db.execute(
            text(
                """
                DELETE FROM tenants t
                WHERE t.id != :tid
                  AND NOT EXISTS (SELECT 1 FROM admins a WHERE a.tenant_id = t.id)
                  AND NOT EXISTS (SELECT 1 FROM oc_panels p WHERE p.tenant_id = t.id)
                """
            ),
            {"tid": PRESERVE_TENANT_ID},
        )
        await db.commit()

        report["admins_final"] = (await db.execute(text("SELECT COUNT(*) FROM admins"))).scalar()
        report["users_final"] = (await db.execute(text("SELECT COUNT(*) FROM users"))).scalar()
        report["tenants_final"] = (await db.execute(text("SELECT COUNT(*) FROM tenants"))).scalar()
        report["workspaces_final"] = (await db.execute(text("SELECT COUNT(*) FROM workspaces"))).scalar()
        report["oc_panels_final"] = (await db.execute(text("SELECT COUNT(*) FROM oc_panels"))).scalar()
        report["panel_86"] = (
            await db.execute(
                text("SELECT id, name FROM oc_panels WHERE id IN (5188, 5189)")
            )
        ).all()

    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(dry_run=args.dry_run))
    for k, v in report.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
