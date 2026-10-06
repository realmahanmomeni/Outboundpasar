"""User-facing OC panel configuration counts (subscription-backed, not catalog size)."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanel, OCPanelDestinationHost
from app.services.oc_connection_credentials import call_oc_panel_api
from app.services.oc_integration_client import OcIntegrationApiError
from app.services.oc_share_link import decode_subscription_links, is_importable_configuration_link
from app.services.oc_upstream_subscription import fetch_upstream_subscription_body


async def count_materialized_destination_configs(db: AsyncSession, panel_id: int) -> int:
    """Destination hosts materialized from the panel subscription (post-import pipeline)."""
    value = await db.scalar(
        select(func.count())
        .select_from(OCPanelDestinationHost)
        .where(
            OCPanelDestinationHost.panel_id == panel_id,
            OCPanelDestinationHost.source_missing.is_(False),
            OCPanelDestinationHost.locally_hidden.is_(False),
        )
    )
    return int(value or 0)


async def count_importable_links_in_subscription_body(body: str) -> int:
    links = decode_subscription_links(body or "")
    return sum(1 for link in links if is_importable_configuration_link(link))


async def count_importable_links_from_subscription_url(subscription_url: str) -> int:
    raw = await fetch_upstream_subscription_body(subscription_url.strip(), "links")
    text = raw if isinstance(raw, str) else raw.decode()
    return await count_importable_links_in_subscription_body(text)


async def discovery_subscription_importable_count(db: AsyncSession, panel: OCPanel) -> int | None:
    """Live discovery-user subscription decode (same filters as host materialization)."""
    test_user_id = (panel.test_user_id or "").strip()
    if not test_user_id:
        return None
    path = f"/v1/integration/panels/{panel.source_panel_id}/users/{test_user_id}"
    try:
        user_data = await call_oc_panel_api(db, panel, "GET", path, None)
    except OcIntegrationApiError:
        return None
    subscription_url = str((user_data or {}).get("subscription_url") or "").strip()
    if not subscription_url:
        return None
    try:
        return await count_importable_links_from_subscription_url(subscription_url)
    except Exception:
        return None


async def panel_available_config_count(db: AsyncSession, panel: OCPanel) -> int:
    """
    Importable configuration links from the discovery user's current subscription.

    Falls back to materialized destination hosts when the subscription cannot be read.
    """
    live = await discovery_subscription_importable_count(db, panel)
    if live is not None:
        return live
    return await count_materialized_destination_configs(db, panel.id)


async def destination_config_counts_by_panel(db: AsyncSession, panel_ids: list[int]) -> dict[int, int]:
    """Materialized destination host counts (legacy helper; prefer available_config_counts_by_panel)."""
    if not panel_ids:
        return {}
    rows = (
        await db.execute(
            select(OCPanelDestinationHost.panel_id, func.count())
            .where(
                OCPanelDestinationHost.panel_id.in_(panel_ids),
                OCPanelDestinationHost.source_missing.is_(False),
                OCPanelDestinationHost.locally_hidden.is_(False),
            )
            .group_by(OCPanelDestinationHost.panel_id)
        )
    ).all()
    return {int(pid): int(count) for pid, count in rows}


async def available_config_counts_by_panel(
    db: AsyncSession,
    panels: list[OCPanel],
    *,
    max_concurrency: int = 6,
) -> dict[int, int]:
    if not panels:
        return {}

    sem = asyncio.Semaphore(max(1, max_concurrency))
    materialized = await destination_config_counts_by_panel(db, [p.id for p in panels])

    async def _one(panel: OCPanel) -> tuple[int, int]:
        async with sem:
            live = await discovery_subscription_importable_count(db, panel)
        if live is not None:
            return panel.id, live
        return panel.id, materialized.get(panel.id, 0)

    pairs = await asyncio.gather(*[_one(p) for p in panels])
    return {int(pid): int(count) for pid, count in pairs}
