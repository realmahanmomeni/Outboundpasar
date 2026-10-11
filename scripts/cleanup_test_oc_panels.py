#!/usr/bin/env python3
"""Remove OCPanel rows bound to test/mock OC integrations (shared DB hygiene).

Uses ``remove_imported_panel`` — never raw DELETE on oc_panels.
Production integrations (e.g. outboundcenter.shop) are never touched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.models_oc import OCIntegration, OCPanel
from app.node.oc_sync import remove_imported_panel
from app.services.oc_test_integration import is_test_oc_integration_base_url


async def run(apply: bool) -> dict:
    report: dict = {"dry_run": not apply, "panels_removed": 0, "panel_ids": [], "integrations_seen": 0}
    async with GetDB() as db:
        panels = (
            await db.execute(
                select(OCPanel)
                .options(selectinload(OCPanel.integration))
                .order_by(OCPanel.id)
            )
        ).scalars().all()
        to_remove: list[OCPanel] = []
        for panel in panels:
            integ = panel.integration
            if integ is None:
                continue
            if is_test_oc_integration_base_url(integ.base_url):
                to_remove.append(panel)
        report["integrations_seen"] = len({p.integration_id for p in to_remove})
        report["panel_ids"] = [p.id for p in to_remove]
        if apply:
            for panel in to_remove:
                await remove_imported_panel(db, panel)
                report["panels_removed"] += 1
            await db.commit()
        else:
            report["panels_removed"] = len(to_remove)
    return report


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Execute cleanup (default is dry-run)")
    args = p.parse_args()
    report = asyncio.run(run(apply=args.apply))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
