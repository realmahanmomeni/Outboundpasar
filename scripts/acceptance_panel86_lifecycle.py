"""Lifecycle acceptance: manual host delete, panel delete, reimport (application paths only)."""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, load_admin_attrs
from app.db.models import Admin, Group, ProxyHost, ProxyInbound, User
from app.db.models import inbounds_groups_association
from app.db.models_oc import OCPanel, OCPanelDestinationHost
from app.operation import OperatorType
from app.operation.host import HostOperation
from app.routers import panel as panel_router
from scripts.acceptance_panel86_restore import (
    ADMIN_ID,
    GROUP_ID,
    SOURCE_SUBSCRIPTION_ID,
    TENANT_ID,
    assign_group_destinations,
    sync_users,
    verify_subscriptions,
    import_and_sync_panel,
)


async def _admin_details(db):
    admin = (await db.execute(select(Admin).where(Admin.id == ADMIN_ID))).scalar_one()
    await load_admin_attrs(admin, load_users=False, load_usage_logs=False, load_role=True)
    return build_admin_details(admin)


async def find_panel86_id() -> int:
    async with GetDB() as db:
        panel = (
            await db.execute(
                select(OCPanel)
                .where(
                    OCPanel.tenant_id == TENANT_ID,
                    OCPanel.workspace_id == 1,
                    OCPanel.source_panel_id == str(SOURCE_SUBSCRIPTION_ID),
                )
                .order_by(OCPanel.id.desc())
            )
        ).scalars().first()
        if panel is None:
            raise RuntimeError("Panel 86 (source 1506) not found")
        return panel.id


async def pick_delete_tag(panel_id: int) -> str:
    async with GetDB() as db:
        g = (
            await db.execute(
                select(Group).where(Group.id == GROUP_ID)
            )
        ).scalar_one()
        assoc = (
            await db.execute(
                select(ProxyInbound.tag)
                .join(inbounds_groups_association, inbounds_groups_association.c.inbound_id == ProxyInbound.id)
                .where(inbounds_groups_association.c.group_id == GROUP_ID)
            )
        ).scalars().all()
        oc_tags = sorted(t for t in assoc if t.startswith(f"oc_{panel_id}_d_"))
        if len(oc_tags) < 2:
            raise RuntimeError(f"Need >=2 OC tags on group for lifecycle test, got {oc_tags}")
        return oc_tags[0]


async def test_manual_host_delete(panel_id: int, delete_tag: str) -> None:
    async with GetDB() as db:
        admin = await _admin_details(db)
        host = (
            await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == delete_tag))
        ).scalar_one()
        host_id = host.id
        print(f"manual_delete host_id={host_id} tag={delete_tag}")
        op = HostOperation(OperatorType.API)
        await op.remove_host(db, host_id, admin)
        await db.commit()
    async with GetDB() as db:
        assert (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == delete_tag))).scalar_one_or_none() is None
        assert (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == delete_tag))).scalar_one_or_none() is None
        assert (
            await db.execute(
                select(OCPanelDestinationHost).where(
                    OCPanelDestinationHost.panel_id == panel_id,
                    OCPanelDestinationHost.virtual_inbound_tag == delete_tag,
                )
            )
        ).scalar_one_or_none() is None
        assoc = (
            await db.execute(
                select(ProxyInbound.tag)
                .join(inbounds_groups_association, inbounds_groups_association.c.inbound_id == ProxyInbound.id)
                .where(inbounds_groups_association.c.group_id == GROUP_ID)
            )
        ).scalars().all()
        assert delete_tag not in assoc
        assert "Shadowsocks TCP" in assoc
        print("manual_host_delete OK group_refs_clean")
        ok = await verify_subscriptions(panel_id)
        print(f"post_manual_delete_subscription_ok={ok}")


async def test_panel_disconnect(panel_id: int) -> None:
    async with GetDB() as db:
        admin = await _admin_details(db)
        ctx = (admin.username, False, admin)
        await panel_router.delete_panel(panel_id, db=db, user_context=ctx)
        await db.commit()
    async with GetDB() as db:
        assert await db.get(OCPanel, panel_id) is None
        dest = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel_id))
        ).scalars().all()
        assert dest == []
        orphans = (
            await db.execute(select(ProxyInbound.tag).where(ProxyInbound.tag.like(f"oc_{panel_id}_d_%")))
        ).scalars().all()
        assert orphans == []
        native = (
            await db.execute(
                select(ProxyInbound.tag)
                .join(inbounds_groups_association, inbounds_groups_association.c.inbound_id == ProxyInbound.id)
                .where(inbounds_groups_association.c.group_id == GROUP_ID)
            )
        ).scalars().all()
        assert "Shadowsocks TCP" in native
        assert not any(t.startswith(f"oc_{panel_id}_") for t in native)
        print("panel_disconnect OK")


async def test_reimport_and_users() -> int:
    panel_id = await import_and_sync_panel()
    tags_before = await assign_group_destinations(panel_id)
    stale = [t for t in tags_before if t.startswith("oc_") and not t.startswith(f"oc_{panel_id}_")]
    print(f"reimport stale_oc_tags_in_group={stale}")
    from sqlalchemy.orm import selectinload
    from app.db.crud.user import get_users_by_ids
    from app.node.oc_sync import enqueue_oc_user_sync_many

    async with GetDB() as db:
        users = await get_users_by_ids(db, (3138, 3139))
        for u in users:
            u = (
                await db.execute(
                    select(User).options(selectinload(User.groups).selectinload(Group.inbounds)).where(User.id == u.id)
                )
            ).scalar_one()
        await enqueue_oc_user_sync_many(db, users)
        await db.commit()
    await sync_users(panel_id)
    ok = await verify_subscriptions(panel_id)
    print(f"reimport_users ok={ok} panel_id={panel_id}")
    return panel_id if ok else 0


async def main() -> int:
    panel_id = await find_panel86_id()
    print(f"lifecycle_start panel_id={panel_id}")
    await assign_group_destinations(panel_id)
    delete_tag = await pick_delete_tag(panel_id)
    await test_manual_host_delete(panel_id, delete_tag)
    # Restore deleted destination before panel disconnect test (wizard sync on existing panel).
    from app.routers import integration as integ

    async with GetDB() as db:
        admin = await _admin_details(db)
        ctx = (admin.username, False, admin)
        groups_resp = await integ.get_groups(panel_id=panel_id, db=db, user_context=ctx)
        selected = [g.id for g in groups_resp.groups]
        gn = {g.id: g.name for g in groups_resp.groups}
        await integ.sync_configs_and_hosts(
            panel_id=panel_id,
            req=integ.SyncRequest(selected_group_ids=selected, group_names=gn),
            db=db,
            user_context=ctx,
        )
        await db.commit()
    await assign_group_destinations(panel_id)
    panel_id = await find_panel86_id()
    await assign_group_destinations(panel_id)
    await test_panel_disconnect(panel_id)
    new_id = await test_reimport_and_users()
    return 0 if new_id else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
