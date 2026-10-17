#!/usr/bin/env python3
"""Read-only live check for Rebecca (Panel 14) on Outbound Center + PasarGuard routing.

Prints safe metadata only (no secrets).
"""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select

from app.db import GetDB
from app.db.models_oc import OCPanel, OCIntegration, TenantTelegramConnection, TenantTelegramConnectionStatus
from app.services.oc_integration_client import OcIntegrationApiError, call_oc_api
from app.utils.crypto import decrypt_secret


async def _oc_rebecca_subscriptions(integ, account_id: int, conn_token: str) -> list[dict]:
    data = await call_oc_api(
        integ,
        "GET",
        f"/v1/integration/accounts/{account_id}/panels",
        connection_token=conn_token,
    )
    out = []
    for row in data.get("items") or []:
        if str(row.get("panel_type") or "").upper() == "REBECCA":
            out.append(
                {
                    "subscription_id": row.get("subscription_id"),
                    "oc_panel_id": row.get("panel_id"),
                    "name": row.get("name"),
                    "status": row.get("status"),
                }
            )
    return out


async def run(tenant_id: int | None, oc_account_id: int | None) -> int:
    async with GetDB() as db:
        integ = (await db.execute(select(OCIntegration).where(OCIntegration.is_active == True, OCIntegration.base_url.like("%outbound%")))).scalars().first()
        if integ is None:
            integ = (await db.execute(select(OCIntegration).limit(1))).scalar_one()
        stmt = select(TenantTelegramConnection).where(
            TenantTelegramConnection.status == TenantTelegramConnectionStatus.active.value,
            TenantTelegramConnection.active.is_(True),
            TenantTelegramConnection.oc_connection_token_encrypted.is_not(None),
        )
        if tenant_id is not None:
            stmt = stmt.where(TenantTelegramConnection.tenant_id == tenant_id)
        if oc_account_id is not None:
            stmt = stmt.where(TenantTelegramConnection.oc_account_id == oc_account_id)
        conns = (await db.execute(stmt)).scalars().all()
        report: list[dict] = []
        for conn in conns:
            token = await decrypt_secret(conn.oc_connection_token_encrypted)
            subs = await _oc_rebecca_subscriptions(integ, conn.oc_account_id, token)
            for sub in subs:
                sid = str(sub["subscription_id"])
                pg = (
                    await db.execute(
                        select(OCPanel).where(
                            OCPanel.tenant_id == conn.tenant_id,
                            OCPanel.source_panel_id == sid,
                        )
                    )
                ).scalar_one_or_none()
                cred = False
                if pg:
                    cred = bool(
                        (pg.buyer_panel_url or "").strip()
                        and (pg.buyer_sub_admin_username or "").strip()
                        and (pg.buyer_sub_admin_password_encrypted or "").strip()
                    )
                probes = {}
                for path in (
                    f"/v1/integration/panels/{sid}/groups",
                    f"/v1/integration/panels/{sid}/buyer-sub-admin",
                    f"/v1/integration/panels/{sid}/test-user",
                ):
                    method = "POST" if path.endswith("test-user") else "GET"
                    try:
                        await call_oc_api(integ, method, path, None if method == "GET" else {}, connection_token=token)
                        probes[path] = {"ok": True}
                    except OcIntegrationApiError as exc:
                        probes[path] = {"ok": False, "http": exc.oc_status, "code": exc.code}
                report.append(
                    {
                        "tenant_id": conn.tenant_id,
                        "oc_account_id": conn.oc_account_id,
                        "subscription_id": sub["subscription_id"],
                        "oc_panel_id": sub["oc_panel_id"],
                        "name": sub["name"],
                        "imported_in_pasarguard": pg is not None,
                        "pg_panel_id": pg.id if pg else None,
                        "pg_panel_type": pg.panel_type if pg else None,
                        "credential_present": cred,
                        "oc_probes": probes,
                    }
                )
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tenant-id", type=int, default=None)
    p.add_argument("--oc-account-id", type=int, default=None)
    args = p.parse_args()
    return asyncio.run(run(args.tenant_id, args.oc_account_id))


if __name__ == "__main__":
    raise SystemExit(main())
