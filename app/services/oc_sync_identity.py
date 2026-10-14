"""Identity rotation helpers for OC user-mapping sync (USER_IDENTITY_BLOCKED)."""

from __future__ import annotations

from typing import Any

from app.db.models_oc import OCUserMapping

# Set in OCSyncState.payload after one automatic external_user_id rotation.
IDENTITY_ROTATED_PAYLOAD_KEY = "_identity_rotated"


def mapping_eligible_for_identity_rotation(mapping: OCUserMapping) -> bool:
    """
    Only mappings that never completed a successful provision may rotate.

    Preserves active mappings (Panel #40-style), billing-linked rows, and any
    mapping that already has real synced config metadata.
    """
    if mapping.status == "active":
        return False
    if mapping.external_subscription_url:
        return False
    configs = mapping.last_synced_configs
    if configs and configs != []:
        return False
    return True


def should_rotate_for_identity_blocked(
    *,
    operation: str,
    oc_code: str | None,
    identity_rotated_this_run: bool,
    payload: dict[str, Any],
    mapping: OCUserMapping,
) -> bool:
    return (
        operation == "create"
        and oc_code == "USER_IDENTITY_BLOCKED"
        and not identity_rotated_this_run
        and not payload.get(IDENTITY_ROTATED_PAYLOAD_KEY)
        and mapping_eligible_for_identity_rotation(mapping)
    )


def merge_identity_rotation_payload(
    new_payload: dict[str, Any],
    *,
    prior_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep the one-shot rotation marker when reconcile refreshes job payload."""
    if prior_payload and prior_payload.get(IDENTITY_ROTATED_PAYLOAD_KEY):
        return {**new_payload, IDENTITY_ROTATED_PAYLOAD_KEY: True}
    return new_payload
