"""Tenant-wide assignable hosts vs group-selected inbounds (65 vs 26 class of bugs)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.models import Group, ProxyHost, ProxyInbound, Tenant, TenantStatus
from app.services.assignable_hosts import list_group_host_options


@pytest.mark.asyncio
async def test_group_host_options_subset_of_tenant_inventory():
    async with GetDB() as db:
        tenant = Tenant(name=f"scope_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        inb_a = ProxyInbound(tag=f"native_{uuid.uuid4().hex[:6]}")
        inb_b = ProxyInbound(tag=f"native_{uuid.uuid4().hex[:6]}")
        db.add_all([inb_a, inb_b])
        await db.flush()
        for inb, addr in ((inb_a, "1.1.1.1"), (inb_b, "2.2.2.2")):
            host = ProxyHost(
                remark=f"h_{addr}",
                priority=1,
                address={addr},
                status=[],
                alpn=[],
                port=443,
                path="/",
                allowinsecure=False,
                is_disabled=False,
                tenant_id=tenant.id,
            )
            host.inbound = inb
            db.add(host)
        grp = Group(name=f"g_{uuid.uuid4().hex[:6]}", tenant_id=tenant.id, inbounds=[inb_a])
        db.add(grp)
        await db.commit()

        tenant_opts = await list_group_host_options(db, tenant_id=tenant.id)
        assert len(tenant_opts) >= 2
        await grp.awaitable_attrs.inbounds
        assert len(grp.inbounds) == 1
        assert len(tenant_opts) > len(grp.inbounds)
