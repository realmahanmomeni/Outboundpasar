"""Live E2E check for Panel 86 OC destination subscription (read-only; safe logging)."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from sqlalchemy import select
from app.db import GetDB
from app.db.models import User
from app.db.models_oc import OCPanel, OCPanelDestinationHost, OCUserMapping
from app.services.oc_share_link import decode_subscription_links, parse_share_link, subscription_config_links
from app.services.oc_upstream_subscription import fetch_upstream_subscription_body, validate_upstream_subscription_url
from app.operation.subscription import SubscriptionOperation
from app.subscription.share import generate_subscription


PANEL_ID = 1176  # imported panel "Panel 86 (testpg)"


def _uuid_from_link(link: str) -> str | None:
    parsed = parse_share_link(link)
    if not parsed:
        return None
    base = link.split("#", 1)[0]
    m = re.search(r"://([^@]+)@", base)
    return m.group(1) if m else None


async def main() -> None:
    async with GetDB() as db:
        panel = await db.get(OCPanel, PANEL_ID)
        if panel is None:
            raise SystemExit(f"Panel {PANEL_ID} not found")
        print(f"panel_id={panel.id} name={panel.name!r} sync_status={panel.sync_status!r}")

        mapping = (
            await db.execute(
                select(OCUserMapping)
                .where(OCUserMapping.panel_id == PANEL_ID, OCUserMapping.external_subscription_url.isnot(None))
                .order_by(OCUserMapping.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if mapping is None:
            raise SystemExit("No OC user mapping with external_subscription_url for panel 86")
        user = await db.get(User, mapping.user_id)
        if user is None:
            raise SystemExit("Mapped user missing")
        url = (mapping.external_subscription_url or "").strip()
        validate_upstream_subscription_url(url)
        host = urlparse(url).hostname
        print(f"mapped_user_id={mapping.user_id} username={user.username!r} upstream_host={host!r}")

        body = await fetch_upstream_subscription_body(url, "links")
        raw = decode_subscription_links(body if isinstance(body, str) else body.decode())
        customer_configs = subscription_config_links(raw)
        print(f"customer_config_count={len(customer_configs)}")
        for i, p in enumerate(customer_configs[:6]):
            print(f"  customer[{i}] uuid={_uuid_from_link(p.raw)!r} remark={p.remark!r}")

        dest_hosts = (
            await db.execute(
                select(OCPanelDestinationHost).where(
                    OCPanelDestinationHost.panel_id == PANEL_ID,
                    OCPanelDestinationHost.source_missing.is_(False),
                    OCPanelDestinationHost.locally_hidden.is_(False),
                )
            )
        ).scalars().all()
        if len(dest_hosts) < 2:
            raise SystemExit("Need at least 2 destination hosts on panel 86")
        selected = dest_hosts[:2]
        excluded = dest_hosts[2] if len(dest_hosts) > 2 else None
        for h in selected:
            sid = (h.source_payload or {}).get("oc_source_config_id")
            print(f"selected_host tag={h.virtual_inbound_tag!r} oc_source_config_id={sid!r}")

        mapped_user_id = mapping.user_id
        selected_hosts = list(selected)
        excluded_host = excluded
        customer_raw = raw
        inbounds = await user.inbounds()
        print(f"user_inbound_count={len(inbounds)} (expect group with selected destination hosts)")

    async with GetDB() as db:
        user = (
            await db.execute(select(User).where(User.id == mapped_user_id))
        ).scalar_one()
        user_resp = await SubscriptionOperation.validated_user(user)
        central = await generate_subscription(user_resp, "links", as_base64=False)
        if isinstance(central, bytes):
            central = central.decode()
        central_links = decode_subscription_links(central)
        print(f"central_config_count={len(central_links)}")
        for i, link in enumerate(central_links[:6]):
            print(f"  central[{i}] uuid={_uuid_from_link(link)!r}")

        selected_sids = {
            str((h.source_payload or {}).get("oc_source_config_id") or "")
            for h in selected_hosts
        }
        excluded_sid = (
            str((excluded_host.source_payload or {}).get("oc_source_config_id") or "")
            if excluded_host
            else ""
        )

        from app.services.oc_share_link import match_upstream_link_for_oc_source_config

        expected_uuids = set()
        for sid in selected_sids:
            if not sid:
                continue
            matched = match_upstream_link_for_oc_source_config(customer_raw, sid, source_name=sid)
            if matched:
                expected_uuids.add(_uuid_from_link(matched))
        central_uuids = {_uuid_from_link(l) for l in central_links}
        central_uuids.discard(None)
        assert central_links, "central subscription empty"
        assert not any("8.8.8.8" in l for l in central_links), "placeholder address"
        assert expected_uuids.issubset(central_uuids), "missing expected customer UUIDs"
        if excluded_sid:
            ex_link = match_upstream_link_for_oc_source_config(
                customer_raw, excluded_sid, source_name=excluded_sid
            )
            if ex_link:
                ex_base = ex_link.split("#", 1)[0]
                central_bases = {link.split("#", 1)[0] for link in central_links}
                assert ex_base not in central_bases, "excluded config URI present in central subscription"
        discovery_uuid = None
        for h in selected_hosts:
            disc = (h.source_payload or {}).get("subscription_link")
            if disc:
                discovery_uuid = _uuid_from_link(disc)
                break
        if discovery_uuid and central_uuids:
            print(f"discovery_vs_customer_uuid_diff={discovery_uuid not in central_uuids}")
        print("panel86_live_check: OK")


if __name__ == "__main__":
    asyncio.run(main())
