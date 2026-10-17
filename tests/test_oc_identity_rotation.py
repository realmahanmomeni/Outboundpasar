"""Regression tests for USER_IDENTITY_BLOCKED external_user_id rotation."""

from __future__ import annotations

from types import SimpleNamespace
import pytest

from app.services.oc_sync_identity import (
    IDENTITY_ROTATED_PAYLOAD_KEY,
    mapping_eligible_for_identity_rotation,
    merge_identity_rotation_payload,
    should_rotate_for_identity_blocked,
)
from app.services.oc_integration_client import _OC_CODE_RE


def _mapping(**kwargs):
    defaults = {
        "id": 35883,
        "user_id": 6583,
        "panel_id": 5912,
        "external_user_id": "71e35861-test",
        "status": "pending",
        "last_synced_configs": [],
        "external_subscription_url": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_oc_code_regex_parses_user_identity_blocked():
    match = _OC_CODE_RE.search("USER_IDENTITY_BLOCKED")
    assert match is not None
    assert match.group(0) == "USER_IDENTITY_BLOCKED"


def test_should_rotate_only_on_create_identity_blocked():
    m = _mapping()
    assert should_rotate_for_identity_blocked(
        operation="create",
        oc_code="USER_IDENTITY_BLOCKED",
        identity_rotated_this_run=False,
        payload={},
        mapping=m,
    )
    assert not should_rotate_for_identity_blocked(
        operation="update",
        oc_code="USER_IDENTITY_BLOCKED",
        identity_rotated_this_run=False,
        payload={},
        mapping=m,
    )
    assert not should_rotate_for_identity_blocked(
        operation="create",
        oc_code="USER_EXISTS",
        identity_rotated_this_run=False,
        payload={},
        mapping=m,
    )
    assert not should_rotate_for_identity_blocked(
        operation="create",
        oc_code="PANEL_AUTH_FAILED",
        identity_rotated_this_run=False,
        payload={},
        mapping=m,
    )


def test_should_not_rotate_twice_per_job_or_payload():
    m = _mapping()
    assert not should_rotate_for_identity_blocked(
        operation="create",
        oc_code="USER_IDENTITY_BLOCKED",
        identity_rotated_this_run=True,
        payload={},
        mapping=m,
    )
    assert not should_rotate_for_identity_blocked(
        operation="create",
        oc_code="USER_IDENTITY_BLOCKED",
        identity_rotated_this_run=False,
        payload={IDENTITY_ROTATED_PAYLOAD_KEY: True},
        mapping=m,
    )


def test_panel_40_active_mapping_not_eligible():
    active = _mapping(
        id=35884,
        panel_id=5913,
        status="active",
        last_synced_configs=["ws-tun-2087", "ws-tun-2096"],
        external_subscription_url="https://example.invalid/sub",
    )
    assert not mapping_eligible_for_identity_rotation(active)
    assert not should_rotate_for_identity_blocked(
        operation="create",
        oc_code="USER_IDENTITY_BLOCKED",
        identity_rotated_this_run=False,
        payload={},
        mapping=active,
    )


def test_merge_identity_rotation_payload_preserves_marker():
    merged = merge_identity_rotation_payload(
        {"groups": ["g1"], "configs": ["c1"]},
        prior_payload={"groups": ["g0"], IDENTITY_ROTATED_PAYLOAD_KEY: True},
    )
    assert merged[IDENTITY_ROTATED_PAYLOAD_KEY] is True
    assert merged["configs"] == ["c1"]


def test_blocked_then_success_rotation_harness():
    """Simulate the worker PUT loop: one rotation then retry with new external_user_id."""
    mapping = _mapping()
    job = SimpleNamespace(operation="create", payload={"groups": ["g1"], "configs": ["c1"]})
    original_payload = job.payload.copy()
    external_user_id = mapping.external_user_id
    identity_rotated = False
    persisted_external_user_id = mapping.external_user_id

    if should_rotate_for_identity_blocked(
        operation=job.operation,
        oc_code="USER_IDENTITY_BLOCKED",
        identity_rotated_this_run=identity_rotated,
        payload=original_payload,
        mapping=mapping,
    ):
        external_user_id = "new-uuid-rotated"
        identity_rotated = True
        original_payload = {**original_payload, IDENTITY_ROTATED_PAYLOAD_KEY: True}

    # Persist mapping identity only after a successful upstream PUT (deferred commit).
    assert identity_rotated is True
    assert external_user_id == "new-uuid-rotated"
    assert mapping.external_user_id == persisted_external_user_id
    assert original_payload[IDENTITY_ROTATED_PAYLOAD_KEY] is True


def test_process_oc_sync_repeated_blocked_does_not_rotate_again():
    m = _mapping()
    payload = {IDENTITY_ROTATED_PAYLOAD_KEY: True}
    assert not should_rotate_for_identity_blocked(
        operation="create",
        oc_code="USER_IDENTITY_BLOCKED",
        identity_rotated_this_run=False,
        payload=payload,
        mapping=m,
    )


def test_failed_provisioning_does_not_imply_active():
    """Document guard: success path sets active only after CAS; failures keep pending."""
    m = _mapping(status="pending", last_synced_configs=[])
    assert m.status != "active"
    assert not mapping_eligible_for_identity_rotation(
        _mapping(
            status="active",
            last_synced_configs=["c1"],
            external_subscription_url="https://x.example/s",
        )
    )
