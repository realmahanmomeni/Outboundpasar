"""BUG1 hotfix: OC PUT groups from group_mapping; safe destination catalog resolution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.node.oc_sync import (
    build_oc_user_put_payload,
    desired_oc_group_ids,
    legacy_panel_groups_fallback_is_allowed,
    _source_group_ids_from_group_mapping,
)
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
    def __init__(self, values, scalar=None):
        self._values = values
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._values

    def scalar_one_or_none(self):
        return self._scalar


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


def test_legacy_panel_groups_fallback_is_blocked():
    assert legacy_panel_groups_fallback_is_allowed() is False


@pytest.mark.asyncio
async def test_build_oc_user_put_payload_does_not_use_legacy_fallback_when_groups_unresolved():
    db = AsyncMock()
    with patch(
        "app.node.oc_sync.desired_oc_group_ids",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.node.oc_sync.selected_oc_panel_group_ids",
        new=AsyncMock(return_value=["g1", "g2"]),
    ) as selected_mock:
        payload = await build_oc_user_put_payload(db, panel_id=6454, config_ids=["cfg-x"])
    assert payload == {"groups": [], "configs": ["cfg-x"]}
    selected_mock.assert_not_called()


@pytest.mark.asyncio
async def test_desired_oc_group_ids_live_oc_fetch_when_local_mapping_empty():
    db = AsyncMock()
    cfg = SimpleNamespace(
        source_payload={"group_mapping": {"supported": True, "groups": []}},
    )
    panel = SimpleNamespace(id=6454, source_panel_id="1513", integration=SimpleNamespace())
    db.execute = AsyncMock(
        side_effect=[
            MagicMockResult([]),
            MagicMockResult(["1", "2"]),
            MagicMockResult([cfg]),
            MagicMockResult([], scalar=panel),
        ]
    )
    with patch(
        "app.node.oc_sync.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.services.oc_connection_credentials.call_oc_panel_api",
        new=AsyncMock(
            return_value={
                "configs": [
                    {
                        "id": "cfg-cdn",
                        "group_mapping": {"supported": True, "groups": ["2"]},
                    }
                ]
            }
        ),
    ) as live_mock:
        groups = await desired_oc_group_ids(db, panel_id=6454, config_ids=["cfg-cdn"])
    assert groups == ["2"]
    live_mock.assert_awaited_once()


def test_source_group_mapping_intersects_selected_panel_groups_only():
    selected = {"g-a", "g-b"}
    assert _source_group_ids_from_group_mapping(
        {"supported": True, "groups": ["g-a", "g-z"]},
        selected,
    ) == {"g-a"}


class MagicMockScalars:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows
