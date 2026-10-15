"""BUG1 hotfix: OC PUT groups from group_mapping; safe destination catalog resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.node.oc_sync import build_oc_user_put_payload, desired_oc_group_ids
from app.services.oc_destination_runtime import source_config_ids_for_panel_tags
from app.services.oc_share_link import match_link_for_config, parse_share_link


@pytest.mark.asyncio
async def test_desired_oc_group_ids_from_group_mapping_when_panel_group_id_null():
    db = AsyncMock()
    cfg = SimpleNamespace(
        source_payload={
            "group_mapping": {"supported": True, "groups": ["g-se"]},
        }
    )
    db.execute = AsyncMock(
        side_effect=[
            MagicMockResult([]),  # join path empty
            MagicMockResult(["g-se", "g-other"]),
            MagicMockResult([cfg]),
        ]
    )
    groups = await desired_oc_group_ids(db, panel_id=6454, config_ids=["cfg-cdn"])
    assert groups == ["g-se"]


class MagicMockResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


@pytest.mark.asyncio
async def test_build_oc_user_put_payload_includes_groups_without_panel_group_fk():
    db = AsyncMock()
    with patch(
        "app.node.oc_sync.desired_oc_group_ids",
        new=AsyncMock(return_value=["g-se"]),
    ):
        payload = await build_oc_user_put_payload(db, panel_id=6454, config_ids=["cfg-cdn"])
    assert payload == {"groups": ["g-se"], "configs": ["cfg-cdn"]}


@pytest.mark.asyncio
async def test_build_oc_user_put_payload_still_uses_config_derived_groups_not_all_panel_groups():
    db = AsyncMock()
    with patch(
        "app.node.oc_sync.desired_oc_group_ids",
        new=AsyncMock(return_value=["g1"]),
    ), patch(
        "app.node.oc_sync.selected_oc_panel_group_ids",
        new=AsyncMock(return_value=["g1", "g2"]),
    ) as selected_mock:
        payload = await build_oc_user_put_payload(db, panel_id=1, config_ids=["cfg-a"])

    assert payload["groups"] == ["g1"]
    assert payload["configs"] == ["cfg-a"]
    selected_mock.assert_not_called()


@pytest.mark.asyncio
async def test_source_config_ids_ignores_fuzzy_name_match_without_oc_source_config_id():
    dest_tag = "oc_6454_d_fp1234567890abcd"
    row = SimpleNamespace(
        source_payload={
            "subscription_link": "vless://uuid@1.1.1.1:443?security=reality#SE%20%E2%80%A2%20CDN",
        }
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMockScalars([row]))

    ids = await source_config_ids_for_panel_tags(db, 6454, [dest_tag])
    assert ids == []


def test_cdn_destination_remark_does_not_fuzzy_match_httpup_catalog_name():
    """Regression: CDN remark must not map to unrelated Httpup catalog id via token heuristics."""
    link_cdn = "vless://a@1.1.1.1:443?security=reality#SE%20%E2%80%A2%20CDN"
    parsed_cdn = parse_share_link(link_cdn)
    assert parsed_cdn is not None
    index = {k: parsed_cdn for k in parsed_cdn.match_keys()}
    match = match_link_for_config(
        index,
        source_config_id="Httpup",
        source_name="Httpup",
        candidates=[parsed_cdn],
    )
    assert match is None


class MagicMockScalars:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows
