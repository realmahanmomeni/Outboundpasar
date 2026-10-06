"""OCUserMapping lifecycle helpers (desired vs runtime-active)."""

from sqlalchemy import and_

from app.db.models_oc import OCUserMapping

OC_MAPPING_STATUS_ACTIVE = "active"
OC_MAPPING_STATUS_PENDING = "pending"
OC_MAPPING_STATUS_DELETED = "deleted"


def resolve_oc_sync_operation(
    mapping: OCUserMapping,
    *,
    is_new_mapping: bool,
    last_configs: list[str] | None,
) -> str:
    """
    Choose create vs update for the OC integration PUT.

    Re-provisioning after a successful delete uses create semantics even though the
    mapping row still exists with last_synced_configs=[].
    """
    if is_new_mapping or last_configs is None:
        return "create"
    if mapping.status == OC_MAPPING_STATUS_DELETED or last_configs == []:
        return "create"
    return "update"


def mapping_needs_subscription_url_repair(mapping: OCUserMapping, desired_configs: frozenset[str]) -> bool:
    """Active mapping with matching configs but missing/invalid stored subscription URL."""
    from app.services.oc_reconcile import normalize_config_set, stored_subscription_url_valid

    if mapping.status != OC_MAPPING_STATUS_ACTIVE:
        return False
    if normalize_config_set(mapping.last_synced_configs) != desired_configs:
        return False
    return not stored_subscription_url_valid(mapping.external_subscription_url)


def oc_user_mapping_runtime_active_criteria():
    """
    A mapping is runtime-active only after OC provisioning succeeded at least once.

    Pending/failed/deleted mappings must not drive node subscription or traffic paths.
    """
    return and_(
        OCUserMapping.status == OC_MAPPING_STATUS_ACTIVE,
        OCUserMapping.last_synced_configs.is_not(None),
    )
