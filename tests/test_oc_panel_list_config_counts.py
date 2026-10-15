"""OPT 2: Panels list must not perform live subscription fetches per panel."""

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
