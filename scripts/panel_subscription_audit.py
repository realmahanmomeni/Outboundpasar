"""Read-only OC panel subscription pipeline audit (stdout JSON)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import select

from app.db import GetDB
from app.db.models_oc import OCPanel
from app.services.oc_panel_config_audit import audit_panel_configuration_pipeline


async def main() -> int:
    parser = argparse.ArgumentParser(description="Audit OC panel config pipeline stages")
    parser.add_argument("--panel-id", type=int, help="Local OCPanel.id")
    parser.add_argument("--source-panel-id", type=str, help="OC source_panel_id")
    parser.add_argument("--name-like", type=str, help="Panel name ILIKE pattern (e.g. %%Panel 40%%)")
    args = parser.parse_args()

    async with GetDB() as db:
        stmt = select(OCPanel)
        if args.panel_id:
            stmt = stmt.where(OCPanel.id == args.panel_id)
        elif args.source_panel_id:
            stmt = stmt.where(OCPanel.source_panel_id == str(args.source_panel_id))
        elif args.name_like:
            stmt = stmt.where(OCPanel.name.ilike(args.name_like))
        else:
            print("Specify --panel-id, --source-panel-id, or --name-like", file=sys.stderr)
            return 2

        panels = (await db.execute(stmt)).scalars().all()
        if not panels:
            print("No matching panels", file=sys.stderr)
            return 1

        reports = []
        for panel in panels:
            report = await audit_panel_configuration_pipeline(db, panel)
            reports.append(report.as_dict())
        print(json.dumps(reports if len(reports) > 1 else reports[0], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
