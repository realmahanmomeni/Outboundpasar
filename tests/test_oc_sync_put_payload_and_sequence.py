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


def test_process_oc_sync_put_loop_does_not_break_after_first_success():
    """Regression guard: worker must not exit the PUT loop after the first successful body."""
    from pathlib import Path

    source = Path("app/jobs/process_oc_sync.py").read_text(encoding="utf-8")
    marker = "for idx, body in enumerate(put_bodies):"
    start = source.index(marker)
    loop_chunk = source[start : start + 2500]
    assert "last_put_err = None\n                                    break" not in source
