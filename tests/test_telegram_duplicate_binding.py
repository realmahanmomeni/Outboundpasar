"""Telegram admin binding fails closed on duplicate telegram_id."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import get_admin_by_telegram_id
from app.db.models import Admin, AdminRole, Tenant, TenantStatus


@pytest.mark.asyncio
async def test_duplicate_telegram_id_denies_binding():
    tg_id = 900000001
    async with GetDB() as db:
        tenant = Tenant(name=f"tg_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        a1 = Admin(username=f"a1_{uuid.uuid4().hex[:8]}", hashed_password="x", role_id=role.id, tenant_id=tenant.id, telegram_id=tg_id)
        a2 = Admin(username=f"a2_{uuid.uuid4().hex[:8]}", hashed_password="x", role_id=role.id, tenant_id=tenant.id, telegram_id=tg_id)
        db.add(a1)
        db.add(a2)
        await db.commit()

        bound = await get_admin_by_telegram_id(db, tg_id, load_users=False, load_usage_logs=False)
        assert bound is None
