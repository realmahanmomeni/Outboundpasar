"""OPT 2: optional materialized-only count path (list endpoint uses live discovery by default)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_available_config_counts_by_panel_skips_live_when_disabled():
    from types import SimpleNamespace

    from app.services.oc_panel_available_configs import available_config_counts_by_panel

    panel = SimpleNamespace(id=10)
    with patch(
        "app.services.oc_panel_available_configs.destination_config_counts_by_panel",
        new=AsyncMock(return_value={10: 7}),
    ) as materialized_mock, patch(
        "app.services.oc_panel_available_configs.discovery_subscription_importable_count",
        new=AsyncMock(),
    ) as live_mock:
        counts = await available_config_counts_by_panel(None, [panel], live_subscription=False)

    assert counts == {10: 7}
    materialized_mock.assert_awaited_once()
    live_mock.assert_not_called()


def _panel_stub(panel_id: int):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=panel_id,
        integration_id=1,
        name="p",
        source_panel_id="1",
        purchaser_identity="buyer",
        sync_status="connected",
        multiplier=1.0,
        test_user_id=None,
        last_sync_at=None,
        created_at=None,
        updated_at=None,
    )


@pytest.mark.asyncio
async def test_list_panels_default_uses_live_config_counts(monkeypatch):
    from unittest.mock import MagicMock

    from app.routers.panel import list_panels

    panel = _panel_stub(1)
    captured: dict = {}

    async def _fake_counts(db, panels, *, live_subscription=True, **kwargs):
        captured["live_subscription"] = live_subscription
        return {1: 3}

    monkeypatch.setattr(
        "app.routers.panel.available_config_counts_by_panel",
        _fake_counts,
    )

    async def _fake_execute(_stmt):
        result = MagicMock()
        result.scalars.return_value.all.return_value = [panel]
        return result

    db = MagicMock()
    db.execute = _fake_execute

    await list_panels(
        db=db,
        user_context=("buyer", True, None),
        config_counts_source="live",
    )
    assert captured["live_subscription"] is True


@pytest.mark.asyncio
async def test_list_panels_materialized_query_skips_live(monkeypatch):
    from unittest.mock import MagicMock

    from app.routers.panel import list_panels

    panel = _panel_stub(2)
    captured: dict = {}

    async def _fake_counts(db, panels, *, live_subscription=True, **kwargs):
        captured["live_subscription"] = live_subscription
        return {2: 11}

    monkeypatch.setattr(
        "app.routers.panel.available_config_counts_by_panel",
        _fake_counts,
    )

    async def _fake_execute(_stmt):
        result = MagicMock()
        result.scalars.return_value.all.return_value = [panel]
        return result

    db = MagicMock()
    db.execute = _fake_execute

    rows = await list_panels(
        db=db,
        user_context=("buyer", True, None),
        config_counts_source="materialized",
    )
    assert captured["live_subscription"] is False
    assert rows[0].configs_count == 11


@pytest.mark.asyncio
async def test_materialized_list_path_avoids_per_panel_live_latency():
    """Regression guard: list materialized mode must not invoke live discovery."""
    import asyncio
    import time
    from types import SimpleNamespace

    from app.services.oc_panel_available_configs import available_config_counts_by_panel

    panels = [_panel_stub(i) for i in range(12)]

    async def slow_live(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return 99

    with patch(
        "app.services.oc_panel_available_configs.destination_config_counts_by_panel",
        new=AsyncMock(return_value={p.id: 1 for p in panels}),
    ), patch(
        "app.services.oc_panel_available_configs.discovery_subscription_importable_count",
        new=slow_live,
    ):
        start = time.perf_counter()
        counts = await available_config_counts_by_panel(
            None,
            panels,
            live_subscription=False,
        )
        elapsed = time.perf_counter() - start

    assert len(counts) == 12
    assert elapsed < 0.2
