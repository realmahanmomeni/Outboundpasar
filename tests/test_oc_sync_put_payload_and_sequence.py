"""BUG 1: sync payload must not widen groups; worker must apply all PUT bodies."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.node.oc_sync import build_oc_user_put_payload, selected_oc_panel_group_ids


@pytest.mark.asyncio
async def test_build_oc_user_put_payload_does_not_fallback_to_all_import_groups():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=lambda: ["1", "2"]))))

    with patch(
        "app.node.oc_sync.desired_oc_group_ids",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.node.oc_sync.selected_oc_panel_group_ids",
        new=AsyncMock(return_value=["1", "2"]),
    ) as selected_mock:
        payload = await build_oc_user_put_payload(db, panel_id=6454, config_ids=[])

    assert payload == {"groups": [], "configs": []}
    selected_mock.assert_not_called()


@pytest.mark.asyncio
async def test_build_oc_user_put_payload_resolves_groups_from_config_ids():
    db = AsyncMock()
    with patch(
        "app.node.oc_sync.desired_oc_group_ids",
        new=AsyncMock(return_value=["g1"]),
    ), patch(
        "app.node.oc_sync.selected_oc_panel_group_ids",
        new=AsyncMock(return_value=["g1", "g2"]),
    ) as selected_mock:
        payload = await build_oc_user_put_payload(db, panel_id=1, config_ids=["cfg-a"])

    assert payload == {"groups": ["g1"], "configs": ["cfg-a"]}
    selected_mock.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_destination_config_ids_from_virtual_tags():
    from app.node.oc_sync import resolve_destination_config_ids_for_panel_tags

    tags = ["oc_6454_d_ac2d9f8e575107bc", "native-inbound"]
    ids = await resolve_destination_config_ids_for_panel_tags(AsyncMock(), 6454, tags)
    assert ids == ["ac2d9f8e575107bc"]


@pytest.mark.asyncio
async def test_resolve_desired_sync_keys_merges_catalog_and_destination():
    from app.node.oc_sync import resolve_desired_sync_keys_for_panel

    with patch(
        "app.node.oc_sync.resolve_desired_config_ids_for_panel",
        new=AsyncMock(return_value=["inbound-a"]),
    ), patch(
        "app.node.oc_sync.resolve_destination_config_ids_for_panel_tags",
        new=AsyncMock(return_value=["fp1"]),
    ):
        keys = await resolve_desired_sync_keys_for_panel(AsyncMock(), 1, ["oc_1_d_fp1"])
    assert keys == ["fp1", "inbound-a"]


def test_destination_filter_allows_fingerprint_in_synced_without_catalog_id():
    from app.services.oc_destination_runtime import destination_config_allowed_by_sync

    assert destination_config_allowed_by_sync(
        oc_source_config_id="",
        panel_id=6454,
        destination_config_id="ac2d9f8e575107bc",
        synced={"ac2d9f8e575107bc"},
    )


def test_destination_filter_denies_catalog_host_when_synced_empty():
    from app.services.oc_destination_runtime import destination_config_allowed_by_sync

    assert not destination_config_allowed_by_sync(
        oc_source_config_id="inbound-1",
        panel_id=1,
        destination_config_id="fpabc",
        synced=set(),
    )


def test_last_synced_configs_after_put_requires_applied_match_payload():
    from app.services.oc_reconcile import last_synced_configs_after_successful_put

    with pytest.raises(ValueError, match="do not match job payload"):
        last_synced_configs_after_successful_put(
            {"configs": ["cfg-a"], "sync_keys": ["cfg-a"]},
            ["cfg-b"],
        )


def test_last_synced_configs_after_put_persists_sync_keys_with_destination_only():
    from app.services.oc_reconcile import last_synced_configs_after_successful_put

    out = last_synced_configs_after_successful_put(
        {"configs": [], "sync_keys": ["fp-dest"], "destination_sync_keys": ["fp-dest"]},
        [],
    )
    assert out == ["fp-dest"]


def test_last_synced_configs_after_put_rejects_unapplied_catalog_in_sync_keys():
    from app.services.oc_reconcile import last_synced_configs_after_successful_put

    out = last_synced_configs_after_successful_put(
        {
            "configs": ["cfg-a"],
            "sync_keys": ["cfg-a", "cfg-b"],
            "destination_sync_keys": [],
        },
        ["cfg-a"],
    )
    assert out == ["cfg-a"]


def test_last_synced_configs_after_put_legacy_payload_uses_sync_minus_intended_extras():
    from app.services.oc_reconcile import last_synced_configs_after_successful_put

    out = last_synced_configs_after_successful_put(
        {"configs": [], "sync_keys": ["fp-dest"]},
        [],
    )
    assert out == ["fp-dest"]


def test_last_synced_configs_after_put_merges_catalog_and_destination_keys():
    from app.services.oc_reconcile import last_synced_configs_after_successful_put

    out = last_synced_configs_after_successful_put(
        {
            "configs": ["inbound-a"],
            "sync_keys": ["inbound-a", "fp-dest"],
            "destination_sync_keys": ["fp-dest"],
        },
        ["inbound-a"],
    )
    assert out == ["fp-dest", "inbound-a"]


def test_process_oc_sync_put_loop_does_not_break_after_first_success():
    """Regression guard: worker must not exit the PUT loop after the first successful body."""
    from pathlib import Path

    source = Path("app/jobs/process_oc_sync.py").read_text(encoding="utf-8")
    marker = "for idx, body in enumerate(put_bodies):"
    start = source.index(marker)
    loop_chunk = source[start : start + 2500]
    assert "last_put_err = None\n                                    break" not in source
