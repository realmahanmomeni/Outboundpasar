"""Desired-state helpers for OC user mapping reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from app.services.oc_user_mapping_state import (
    OC_MAPPING_STATUS_ACTIVE,
    OC_MAPPING_STATUS_DELETED,
)

if TYPE_CHECKING:
    from app.db.models_oc import OCUserMapping


def normalize_config_set(configs: list[str] | None) -> frozenset[str]:
    if not configs:
        return frozenset()
    return frozenset(c for c in configs if c)


def last_synced_configs_after_successful_put(
    payload: dict,
    last_applied_configs: list[str],
) -> list[str]:
    """
    Keys to store on the mapping after a successful OC user PUT sequence.

    Requires the final PUT ``configs`` to match the job payload. ``sync_keys`` may
    include destination fingerprints for local subscription filtering that are not
    sent in the PUT body; catalog ids in ``sync_keys`` must match the applied set.
    """
    intended = normalize_config_set(payload.get("configs"))
    applied = normalize_config_set(last_applied_configs)
    if intended != applied:
        raise ValueError(
            "OC user PUT configs do not match job payload: "
            f"payload={sorted(intended)} applied={sorted(applied)}"
        )
    sync_keys = payload.get("sync_keys")
    if sync_keys is None:
        return sorted(applied)
    sync_set = normalize_config_set(sync_keys)
    if (sync_set & intended) != applied:
        return sorted(applied)
    dest_allowed = payload.get("destination_sync_keys")
    if dest_allowed is not None:
        extras = sync_set & normalize_config_set(dest_allowed)
    else:
        extras = sync_set - intended
    return sorted(applied | extras)


def stored_subscription_url_valid(url: str | None) -> bool:
    """Local check aligned with process_oc_sync._subscription_url_from (no network I/O)."""
    raw = (url or "").strip()
    return bool(raw.lower().startswith(("http://", "https://")) and len(raw) <= 2048)


def mapping_is_healthy(mapping: OCUserMapping, desired_configs: frozenset[str]) -> bool:
    if mapping.status != OC_MAPPING_STATUS_ACTIVE:
        return False
    if not stored_subscription_url_valid(mapping.external_subscription_url):
        return False
    synced = normalize_config_set(mapping.last_synced_configs)
    return synced == desired_configs


class ReconcileAction(str, Enum):
    NOOP = "OC_RECONCILE_NOOP"
    SKIP = "OC_RECONCILE_SKIP"
    CREATE = "OC_RECONCILE_CREATE"
    REPAIR = "OC_RECONCILE_REPAIR"
    UPDATE = "OC_RECONCILE_UPDATE"
    DELETE = "OC_RECONCILE_DELETE"
    ERROR = "OC_RECONCILE_ERROR"


@dataclass(frozen=True)
class DesiredPanelState:
    panel_id: int
    desired_active: bool
    config_ids: frozenset[str]


def classify_mapping_action(
    mapping: OCUserMapping | None,
    desired: DesiredPanelState,
) -> ReconcileAction:
    if not desired.desired_active:
        if mapping is None:
            return ReconcileAction.NOOP
        if mapping.status == OC_MAPPING_STATUS_DELETED and normalize_config_set(
            mapping.last_synced_configs
        ) == frozenset():
            return ReconcileAction.NOOP
        return ReconcileAction.DELETE

    if mapping is None:
        return ReconcileAction.CREATE

    if mapping.status == OC_MAPPING_STATUS_DELETED or mapping.last_synced_configs == []:
        return ReconcileAction.CREATE

    if mapping.status != OC_MAPPING_STATUS_ACTIVE:
        return ReconcileAction.CREATE

    synced = normalize_config_set(mapping.last_synced_configs)
    if synced != desired.config_ids:
        return ReconcileAction.UPDATE

    if not stored_subscription_url_valid(mapping.external_subscription_url):
        return ReconcileAction.REPAIR

    return ReconcileAction.NOOP
