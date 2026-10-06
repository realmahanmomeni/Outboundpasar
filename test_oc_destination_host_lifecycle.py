"""OC destination host lifecycle: panel delete, manual delete, group integrity."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.models import Group, ProxyHost, ProxyInbound, inbounds_groups_association
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost
from app.node.oc_sync import remove_imported_panel
from app.services.assignable_hosts import list_group_host_options
from app.services.oc_destination_host_lifecycle import (
    teardown_oc_destination_inbound,
    teardown_oc_destination_inbound_tag,
)
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import destination_virtual_inbound_tag


def _discovery_link(i: int) -> str:
    return f"vless://u{i}@10.0.0.{i}:443?encryption=none#Dest%20{i}"


async def _panel_with_destinations(db, *, name: str = "P") -> tuple[OCPanel, list[OCPanelDestinationHost]]:
    intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
    db.add(intg)
    await db.flush()
    panel = OCPanel(
        integration_id=intg.id,
        source_panel_id=f"src_{uuid.uuid4().hex[:8]}",
        purchaser_identity="buyer",
        name=name,
    )
    db.add(panel)
    await db.flush()
    await reconcile_panel_destination_hosts(db, panel, [_discovery_link(1), _discovery_link(2)])
    await db.flush()
    rows = (
        await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
    ).scalars().all()
    return panel, rows


@pytest.mark.asyncio
async def test_panel_delete_removes_destination_hosts_and_group_refs():
    async with GetDB() as db:
        panel, dest_rows = await _panel_with_destinations(db)
        panel_id = panel.id
        tag = destination_virtual_inbound_tag(panel_id, dest_rows[0].destination_config_id)
        inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one()
        native = ProxyInbound(tag=f"native_{uuid.uuid4().hex[:8]}")
        db.add(native)
        await db.flush()
        native_tag = native.tag
        group = Group(name=f"g_{uuid.uuid4().hex[:8]}", inbounds=[inbound, native])
        db.add(group)
        await db.flush()
        group_id = group.id

        await remove_imported_panel(db, panel)
        await db.flush()
        db.expire_all()

        assert (await db.execute(select(OCPanel).where(OCPanel.id == panel_id))).scalar_one_or_none() is None
        assert (
            await db.execute(
                select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel_id)
            )
        ).scalars().all() == []
        assert (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one_or_none() is None
        assert (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == tag))).scalar_one_or_none() is None
        assoc_tags = (
            await db.execute(
                select(ProxyInbound.tag)
                .join(
                    inbounds_groups_association,
                    inbounds_groups_association.c.inbound_id == ProxyInbound.id,
                )
                .where(inbounds_groups_association.c.group_id == group_id)
            )
        ).scalars().all()
        assert tag not in assoc_tags
        assert native_tag in assoc_tags

        await db.rollback()


@pytest.mark.asyncio
async def test_panel_delete_does_not_remove_other_panels_destination_hosts():
    async with GetDB() as db:
        panel_a, rows_a = await _panel_with_destinations(db, name="A")
        panel_b, rows_b = await _panel_with_destinations(db, name="B")
        tag_b = destination_virtual_inbound_tag(panel_b.id, rows_b[0].destination_config_id)

        await remove_imported_panel(db, panel_a)
        await db.flush()

        assert (await db.execute(select(OCPanel).where(OCPanel.id == panel_b.id))).scalar_one_or_none()
        assert (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel_b.id))
        ).scalars().all()
        assert (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag_b))).scalar_one_or_none()

        await db.rollback()


@pytest.mark.asyncio
async def test_manual_destination_delete_cleans_group_and_row():
    async with GetDB() as db:
        panel, dest_rows = await _panel_with_destinations(db)
        panel_id = panel.id
        dest = dest_rows[0]
        dest_config_id = dest.destination_config_id
        tag = destination_virtual_inbound_tag(panel_id, dest_config_id)
        other_tag = destination_virtual_inbound_tag(panel_id, dest_rows[1].destination_config_id)
        inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one()
        other_inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == other_tag))).scalar_one()
        group = Group(name=f"g_{uuid.uuid4().hex[:8]}", inbounds=[inbound, other_inbound])
        db.add(group)
        await db.flush()
        group_id = group.id

        await teardown_oc_destination_inbound_tag(db, tag)
        await db.flush()
        db.expire_all()

        assert (
            await db.execute(
                select(OCPanelDestinationHost).where(
                    OCPanelDestinationHost.panel_id == panel_id,
                    OCPanelDestinationHost.destination_config_id == dest_config_id,
                )
            )
        ).scalar_one_or_none() is None
        assert (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one_or_none() is None
        assoc_tags = (
            await db.execute(
                select(ProxyInbound.tag)
                .join(
                    inbounds_groups_association,
                    inbounds_groups_association.c.inbound_id == ProxyInbound.id,
                )
                .where(inbounds_groups_association.c.group_id == group_id)
            )
        ).scalars().all()
        assert tag not in assoc_tags
        assert other_tag in assoc_tags

        await db.rollback()


@pytest.mark.asyncio
async def test_deleted_destination_not_in_group_picker():
    async with GetDB() as db:
        panel, dest_rows = await _panel_with_destinations(db)
        dest = dest_rows[0]
        tag = destination_virtual_inbound_tag(panel.id, dest.destination_config_id)
        await teardown_oc_destination_inbound(
            db, panel.id, dest.destination_config_id, virtual_inbound_tag=tag
        )
        await db.flush()
        options = await list_group_host_options(db)
        assert tag not in {o.inbound_tag for o in options}
        await db.rollback()


@pytest.mark.asyncio
async def test_native_host_preserved_when_oc_destination_removed():
    async with GetDB() as db:
        panel, dest_rows = await _panel_with_destinations(db)
        tag = destination_virtual_inbound_tag(panel.id, dest_rows[0].destination_config_id)
        native_tag = f"plain_{uuid.uuid4().hex[:8]}"
        native_inbound = ProxyInbound(tag=native_tag)
        db.add(native_inbound)
        await db.flush()
        native_host = ProxyHost(
            remark="native",
            priority=0,
            address={"9.9.9.9"},
            port=443,
            path=None,
            status=[],
            alpn=[],
            is_disabled=False,
            allowinsecure=False,
        )
        native_host.inbound = native_inbound
        db.add(native_host)
        await db.flush()

        await teardown_oc_destination_inbound_tag(db, tag)
        await db.flush()

        assert (await db.execute(select(ProxyHost).where(ProxyHost.id == native_host.id))).scalar_one_or_none()
        assert (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == native_tag))).scalar_one_or_none()

        await db.rollback()


@pytest.mark.asyncio
async def test_group_association_rows_removed_for_deleted_inbound():
    async with GetDB() as db:
        panel, dest_rows = await _panel_with_destinations(db)
        tag = destination_virtual_inbound_tag(panel.id, dest_rows[0].destination_config_id)
        inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one()
        group = Group(name=f"g_{uuid.uuid4().hex[:8]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        inbound_id = inbound.id

        await teardown_oc_destination_inbound_tag(db, tag)
        await db.flush()

        assoc = (
            await db.execute(
                select(inbounds_groups_association).where(
                    inbounds_groups_association.c.inbound_id == inbound_id
                )
            )
        ).first()
        assert assoc is None

        await db.rollback()
