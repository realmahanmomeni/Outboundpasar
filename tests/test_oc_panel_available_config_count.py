"""Focused tests for user-facing OC panel config counts (not catalog size)."""

import base64
from unittest.mock import AsyncMock, patch

import pytest

from app.services.oc_panel_available_configs import (
    count_importable_links_in_subscription_body,
    panel_available_config_count,
)


def _b64_subscription(lines: list[str]) -> str:
    return base64.b64encode("\n".join(lines).encode()).decode()


@pytest.mark.asyncio
async def test_count_importable_links_ignores_junk_and_html():
    good = "vless://uuid@1.2.3.4:443?encryption=none#user%20remark"
    junk = [
        "not a link",
        "<html><body>error</body></html>",
        "http://example.com/page",
    ]
    body = _b64_subscription([good, *junk, good])
    assert await count_importable_links_in_subscription_body(body) == 1


@pytest.mark.asyncio
async def test_panel_available_prefers_live_subscription_over_catalog():
    from app.db.models_oc import OCPanel

    panel = OCPanel(
        integration_id=1,
        source_panel_id="1507",
        purchaser_identity="test",
        name="test-panel",
        test_user_id="disc-user",
    )
    panel.id = 99

    links = [
        "vless://a@1.1.1.1:443?encryption=none#one",
        "vless://b@2.2.2.2:443?encryption=none#two",
    ]
    sub_body = _b64_subscription(links)

    db = AsyncMock()

    with patch(
        "app.services.oc_panel_available_configs.discovery_subscription_importable_count",
        new=AsyncMock(return_value=2),
    ):
        assert await panel_available_config_count(db, panel) == 2

    with patch(
        "app.services.oc_panel_available_configs.discovery_subscription_importable_count",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.oc_panel_available_configs.count_materialized_destination_configs",
        new=AsyncMock(return_value=7),
    ):
        assert await panel_available_config_count(db, panel) == 7


@pytest.mark.asyncio
async def test_catalog_31_vs_subscription_9_style_fixture():
    """Regression: UI must not use catalog row count."""
    catalog_size = 31
    importable = [
        f"vless://id{i}@10.0.0.{i}:443?encryption=none#cfg{i}" for i in range(1, 10)
    ]
    body = _b64_subscription(importable)
    counted = await count_importable_links_in_subscription_body(body)
    assert counted == 9
    assert catalog_size != counted
