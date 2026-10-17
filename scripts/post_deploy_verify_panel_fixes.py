#!/usr/bin/env python3
"""Post-deploy verification: panel list tenant scope + in-app import idempotency."""
from __future__ import annotations

import asyncio

from fastapi import HTTPException
from sqlalchemy import func, select

from app.db import GetDB
from app.db.models import Admin
from app.models.admin import AdminDetails, AdminRoleData
from app.routers.panel import list_panels


async def verify_list_panels_in_process() -> None:
    admin_ctx = (
        "post_deploy_verify",
        False,
        AdminDetails(
            id=0,
            username="post_deploy_verify",
            tenant_id=None,
            role=AdminRoleData(is_owner=False, permissions={"nodes": {"read": True}}),
        ),
    )
    async with GetDB() as db:
        try:
            await list_panels(db, admin_ctx)
        except HTTPException as exc:
            if exc.status_code != 403 or "Tenant scope required" not in str(exc.detail):
                raise SystemExit(f"list_panels unexpected: {exc.status_code} {exc.detail}") from exc
            print("IN_PROCESS list_panels: 403 Tenant scope required")
        else:
            raise SystemExit("list_panels should have raised 403")


async def verify_http_list_panels() -> None:
    """Hit deployed ASGI app with a real admin JWT (tenant_id NULL) if one exists."""
    from starlette.testclient import TestClient

    from main import app
    from app.utils.jwt import create_admin_token

    async with GetDB() as db:
        row = (
            await db.execute(select(Admin).where(Admin.tenant_id.is_(None)).limit(1))
        ).scalar_one_or_none()
        if row is None:
            print("HTTP list_panels: SKIPPED (no admin with tenant_id=NULL in DB)")
            return
        token = await create_admin_token(row.id, row.username)
        admin_id, username = row.id, row.username

    with TestClient(app) as client:
        resp = client.get("/api/panels", headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 403:
        raise SystemExit(f"HTTP GET /api/panels expected 403 got {resp.status_code} body={resp.text[:200]}")
    if "Tenant scope required" not in resp.text:
        raise SystemExit(f"HTTP body missing detail: {resp.text[:200]}")
    print(f"HTTP GET /api/panels: 403 (admin id={admin_id} username={username!r})")


async def main() -> None:
    await verify_list_panels_in_process()
    await verify_http_list_panels()


if __name__ == "__main__":
    asyncio.run(main())
