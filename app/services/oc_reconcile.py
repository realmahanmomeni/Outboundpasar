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
