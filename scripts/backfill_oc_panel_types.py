#!/usr/bin/env python3
"""Backfill NULL OCPanel.panel_type from OC account panel list (dry-run by default)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.db import GetDB
from app.services.oc_panel_type_backfill import (
    backfill_null_panel_types,
    classify_null_panel_types,
    panels_missing_metadata_diagnostic,
)


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Backfill OCPanel.panel_type from Outbound Center")
    parser.add_argument("--tenant-id", type=int, help="Limit to one tenant")
    parser.add_argument("--apply", action="store_true", help="Persist updates (default: dry-run)")
    parser.add_argument("--diagnose", action="store_true", help="List panels that cannot be refreshed")
    parser.add_argument("--classify", action="store_true", help="Summarize NULL panel_type categories")
    args = parser.parse_args()

    async with GetDB() as db:
        if args.classify:
            print(json.dumps(await classify_null_panel_types(db), indent=2))
            return 0
        if args.diagnose:
            rows = await panels_missing_metadata_diagnostic(db)
            print(json.dumps(rows, indent=2))
            return 0
        report = await backfill_null_panel_types(
            db,
            tenant_id=args.tenant_id,
            dry_run=not args.apply,
            allow_revoked=True,
        )
        print(json.dumps(report.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
