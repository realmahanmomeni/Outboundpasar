"""OC destination host lifecycle: repair, idempotency, group validation."""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import func, select

from app.db import GetDB
from app.db.models import Admin, Group, ProxyHost, ProxyInbound, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCPanelDestinationHost
from app.services.oc_inbound_tags import validate_inbound_tags_for_group
from app.services.oc_panel_destination_hosts import (
    ensure_panel_destination_hosts_materialized,
    reconcile_panel_destination_hosts,
)
from app.services.oc_share_link import destination_virtual_inbound_tag, subscription_config_links


def _link(uuid_val: str, remark: str) -> str:
    return f"vless://{uuid_val}@example.com:443?encryption=none#{remark}"


async def _raise_error(message: str, code: int = 400):
    raise AssertionError(f"{code}:{message}")


async def test_reconcile_idempotent_and_restores_virtual_tags():
    raw = [
        "vless://u1@a:1?encryption=none#🇸🇪|CDN",
        "vless://u2@a:2?encryption=none#📡Status|ok",
        "vless://u3@a:3?encryption=none#🇸🇪|DIRECT",
    ]
    async with GetDB() as db:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="sp", purchaser_identity="b", name="P")
        db.add(panel)
        await db.flush()
        catalog_tag = f"oc_{panel.id}_HTTP-UP"
        db.add(ProxyInbound(tag=catalog_tag))
        await db.flush()
        db.add(
            OCPanelConfig(
                panel_id=panel.id,
                source_config_id="HTTP-UP",
                source_name="HTTP-UP",
                virtual_inbound_tag=catalog_tag,
            )
        )
        await db.flush()
        n1 = await reconcile_panel_destination_hosts(db, panel, raw)
        await db.flush()
        count1 = await db.scalar(
            select(func.count()).select_from(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id)
        )
        n2 = await reconcile_panel_destination_hosts(db, panel, raw)
        await db.flush()
        count2 = await db.scalar(
            select(func.count()).select_from(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id)
        )
        assert count1 == count2 == 2
        assert n2 >= 0
        rows = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalars().all()
        for row in rows:
            assert row.virtual_inbound_tag == destination_virtual_inbound_tag(panel.id, row.destination_config_id)
            host = (
                await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == row.virtual_inbound_tag))
            ).scalar_one_or_none()
            assert host is not None
        for row in rows:
            row.virtual_inbound_tag = None
        await db.execute(
            ProxyHost.__table__.delete().where(ProxyHost.inbound_tag.like(f"oc_{panel.id}_d_%"))
        )
        await db.execute(
            ProxyInbound.__table__.delete().where(ProxyInbound.tag.like(f"oc_{panel.id}_d_%"))
        )
        await db.flush()
        repaired = await ensure_panel_destination_hosts_materialized(db, panel)
        assert repaired == len(rows)
        for row in rows:
            assert row.virtual_inbound_tag is not None
        await db.rollback()


async def test_presentation_links_not_destination_hosts():
    raw = [
        "vless://u1@a:1?encryption=none#📡Status|x",
        "vless://u2@a:2?encryption=none#👤user|name",
        "vless://u3@a:3?encryption=none#🇸🇪|CDN",
    ]
    configs = subscription_config_links(raw)
    assert len(configs) == 1
    assert configs[0].remark == "🇸🇪|CDN"


async def test_legacy_catalog_tag_rejected_for_groups():
    async with GetDB() as db:
        try:
            await validate_inbound_tags_for_group(
                db,
                [f"oc_999_HTTP-UP"],
                raise_error=_raise_error,
            )
        except AssertionError as exc:
            assert "Legacy OC catalog" in str(exc)
        else:
            raise AssertionError("expected legacy tag rejection")


async def test_empty_oc_destination_group_allowed():
    async with GetDB() as db:
        await validate_inbound_tags_for_group(db, [], raise_error=_raise_error)


async def test_destination_tag_resolves_not_legacy():
    from app.services.oc_share_link import is_oc_destination_inbound_tag, parse_destination_tag

    legacy = "oc_1206_HTTP-UP"
    dest = destination_virtual_inbound_tag(1206, "abc123")
    assert not is_oc_destination_inbound_tag(legacy)
    assert is_oc_destination_inbound_tag(dest)
    assert parse_destination_tag(dest) == (1206, "abc123")


async def main():
    await test_reconcile_idempotent_and_restores_virtual_tags()
    await test_presentation_links_not_destination_hosts()
    await test_legacy_catalog_tag_rejected_for_groups()
    await test_empty_oc_destination_group_allowed()
    await test_destination_tag_resolves_not_legacy()
    print("test_oc_destination_lifecycle: OK")


if __name__ == "__main__":
    asyncio.run(main())
