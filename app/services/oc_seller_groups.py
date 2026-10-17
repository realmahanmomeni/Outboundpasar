"""Seller groups for OC user provisioning come from the panel import selection.

Source of truth
---------------
``OCPanelGroup.is_selected`` is written by the panel import / "edit import selection" call
(``POST /api/integration/panels/{panel_id}/sync`` with ``selected_group_ids``) and is the same selection
the discovery user is assigned to (``oc_panel_host_subscription``). Every user provisioned on that
panel is created/updated with exactly those source seller group ids. Nothing else is consulted: not the
user's selected hosts/configs, not destination fingerprints, not config names, not a per-user list.

Safety
------
* Groups that were imported but *not* selected are never assigned.
* There is no "all groups" or "first group" fallback.
* When the panel has imported groups but none is selected the seller account is neither created nor
  updated and a rate-limited, actionable warning is logged. Kill switch
  ``OC_SELLER_GROUP_STRICT_SELECTION=0`` restores the legacy "send no groups" behaviour.
* A panel with no imported group rows at all (legacy / never group-imported) has no selection to apply:
  it keeps the legacy payload (config-derived groups only, i.e. none) and is not blocked.
* The applied groups are recorded per seller account (``OCUserMapping.last_synced_group_ids``) only after
  a successful PUT, so a changed import selection is detected as drift and retried by the periodic
  reconcile until it is applied.

Deprecated per-user selection
-----------------------------
``UserPanelBinding.desired_source_group_ids`` and the two read endpoints that exposed it were shipped
before this design and are kept (contract unchanged) but no longer influence provisioning.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanelGroup, OCUserMapping, UserPanelBinding
from app.utils.logger import get_logger

logger = get_logger("oc-seller-groups")

SELLER_GROUP_STRICT_ENV = "OC_SELLER_GROUP_STRICT_SELECTION"

ISSUE_IMPORT_SELECTION_MISSING = "import_selection_missing"

GROUP_SOURCE_PANEL_IMPORT = "panel_import"

_MAPPING_STATUS_DELETED = "deleted"


def strict_seller_group_selection_enabled() -> bool:
    """
    When true (default) a seller account is only created/updated once the panel's import selection
    names at least one source group. Kill switch ``OC_SELLER_GROUP_STRICT_SELECTION=0`` restores the
    legacy behaviour (send no groups, seller default group applies).
    """
    return os.environ.get(SELLER_GROUP_STRICT_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


class OcSellerGroupSelectionError(ValueError):
    """Raised before any seller API call when the panel's group selection is missing or invalid."""

    def __init__(self, message: str, *, issue: str):
        super().__init__(message)
        self.issue = issue


@dataclass(frozen=True)
class PanelGroupInventory:
    panel_id: int
    # source_group_id -> display name, for every imported group
    names: dict[str, str] = field(default_factory=dict)
    # groups selected during panel import (the groups every user of the panel receives)
    eligible_ids: tuple[str, ...] = ()

    @property
    def eligible_set(self) -> frozenset[str]:
        return frozenset(self.eligible_ids)


@dataclass(frozen=True)
class GroupDecision:
    groups: tuple[str, ...]
    issue: str | None = None
    invalid_ids: tuple[str, ...] = ()
    message: str | None = None

    @property
    def blocked(self) -> bool:
        return self.issue is not None


async def load_panel_group_inventory(db: AsyncSession, panel_id: int) -> PanelGroupInventory:
    rows = (
        await db.execute(
            select(OCPanelGroup.source_group_id, OCPanelGroup.source_name, OCPanelGroup.is_selected)
            .where(OCPanelGroup.panel_id == panel_id)
            .order_by(OCPanelGroup.id.asc())
        )
    ).all()
    names: dict[str, str] = {}
    eligible: list[str] = []
    for source_id, name, is_selected in rows:
        sid = str(source_id)
        names[sid] = str(name or sid).strip()
        if is_selected and sid not in eligible:
            eligible.append(sid)
    return PanelGroupInventory(panel_id=panel_id, names=names, eligible_ids=tuple(sorted(eligible)))


def decide_import_groups(inventory: PanelGroupInventory, strict: bool | None = None) -> GroupDecision:
    """
    Pure decision: the source groups a user on this panel receives = the import selection, verbatim.

    Never widened (no unselected group, no all-groups/first-group fallback). Blocked only when groups were
    imported but none is selected (ambiguous: the admin never chose). A panel without any imported group
    row has no selection to apply and keeps the legacy payload (empty groups, not blocked).
    """
    if strict is None:
        strict = strict_seller_group_selection_enabled()
    if inventory.eligible_ids:
        return GroupDecision(groups=tuple(sorted(inventory.eligible_ids)))
    if not strict or not inventory.names:
        return GroupDecision(groups=())
    return GroupDecision(
        groups=(),
        issue=ISSUE_IMPORT_SELECTION_MISSING,
        message=(
            f"Panel {inventory.panel_id} has imported seller groups ("
            + ", ".join(f"{inventory.names.get(g, g)} [{g}]" for g in sorted(inventory.names))
            + ") but none is selected. Re-open the panel import and select the groups to import. "
            "The seller account is not created or updated until then."
        ),
    )


def import_group_payload(decision: GroupDecision) -> dict[str, Any]:
    """Provenance stored in the sync job so the worker can re-validate right before the PUT."""
    return {"source": GROUP_SOURCE_PANEL_IMPORT, "groups": list(decision.groups)}


def group_drift(last_synced_group_ids: list[str] | None, groups: tuple[str, ...] | list[str]) -> bool:
    """
    True when the groups applied to the seller account differ from the current import selection.

    ``None`` means the applied groups are unknown (account created before this tracking existed): that is
    deliberately *not* drift, so deploying never triggers an unannounced bulk resync. Such accounts are
    converged when their configs change or when the admin saves the panel's import selection
    (see ``mark_panel_group_assignments_stale``).
    """
    if last_synced_group_ids is None:
        return False
    return {str(g) for g in last_synced_group_ids} != {str(g) for g in groups}


async def mark_panel_group_assignments_stale(db: AsyncSession, panel_id: int) -> int:
    """
    Admin saved the panel's import selection: accounts whose applied groups are unknown become
    ``[]`` (= the legacy "no explicit groups" state they were really created with), so the periodic
    reconcile queues exactly one group update per account. Idempotent; does not enqueue anything itself.
    """
    result = await db.execute(
        update(OCUserMapping)
        .where(
            OCUserMapping.panel_id == panel_id,
            OCUserMapping.last_synced_group_ids.is_(None),
            OCUserMapping.status != _MAPPING_STATUS_DELETED,
            OCUserMapping.last_synced_at.is_not(None),
        )
        .values(last_synced_group_ids=[])
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def assert_job_groups_still_eligible(
    db: AsyncSession, panel_id: int, groups: list[str] | None
) -> None:
    """Worker-side re-validation: the import selection may have shrunk after the job was queued."""
    wanted = [str(g) for g in (groups or []) if str(g).strip()]
    if not wanted:
        return
    inventory = await load_panel_group_inventory(db, panel_id)
    stray = [g for g in wanted if g not in inventory.eligible_set]
    if stray:
        raise OcSellerGroupSelectionError(
            "Seller group(s) " + ", ".join(stray) + " are no longer selected in this panel's import; "
            "no seller request was sent. The next reconcile queues the update with the current selection.",
            issue=ISSUE_IMPORT_SELECTION_MISSING,
        )


# --------------------------------------------------------------------------- deprecated per-user field


async def load_explicit_binding(db: AsyncSession, user_id: int, panel_id: int) -> UserPanelBinding | None:
    return (
        await db.execute(
            select(UserPanelBinding).where(
                UserPanelBinding.user_id == user_id,
                UserPanelBinding.oc_panel_id == panel_id,
            )
        )
    ).scalar_one_or_none()


def explicit_ids_from_binding(binding: UserPanelBinding | None) -> list[str] | None:
    """Stored (deprecated) per-user value of an enabled binding; informational only."""
    if binding is None or not binding.enabled:
        return None
    raw = binding.desired_source_group_ids
    if raw is None:
        return None
    out: list[str] = []
    for item in raw:
        sid = str(item).strip()
        if sid and sid not in out:
            out.append(sid)
    return out or None


def validate_explicit_selection(
    explicit_ids: list[str] | None, inventory: PanelGroupInventory
) -> tuple[list[str], list[str]]:
    """Split ``explicit_ids`` into (valid, invalid) with respect to the panel's import selection."""
    valid: list[str] = []
    invalid: list[str] = []
    for gid in explicit_ids or []:
        (valid if gid in inventory.eligible_set else invalid).append(gid)
    return valid, invalid


async def build_seller_group_panels(
    db: AsyncSession,
    inbound_tags: list[str],
    *,
    user_id: int | None = None,
    panel_visible: Any = None,
) -> list[dict[str, Any]]:
    """
    Read-only, per OC panel reachable through ``inbound_tags``: imported seller groups, which of them the
    panel import selected (= assigned to every user of the panel) and whether provisioning is blocked
    because no import selection exists.

    ``panel_visible`` is an optional ``async (OCPanel) -> bool`` used by the caller to apply
    tenant/workspace/ownership checks; panels it rejects are omitted.
    """
    from app.db.models_oc import OCPanel
    from app.node.user import _bucket_inbounds

    buckets = _bucket_inbounds(list(inbound_tags))
    panel_ids = sorted(pid for pid, tags in buckets.items() if pid is not None and tags)
    if not panel_ids:
        return []

    strict = strict_seller_group_selection_enabled()
    mappings: dict[int, Any] = {}
    if user_id is not None:
        mappings = {
            m.panel_id: m
            for m in (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == user_id))).scalars()
        }
    out: list[dict[str, Any]] = []
    for panel_id in panel_ids:
        panel = await db.get(OCPanel, panel_id)
        if panel is None:
            continue
        if panel_visible is not None and not await panel_visible(panel):
            continue
        inventory = await load_panel_group_inventory(db, panel_id)
        binding = await load_explicit_binding(db, user_id, panel_id) if user_id is not None else None
        stored = explicit_ids_from_binding(binding)  # deprecated, informational
        decision = decide_import_groups(inventory, strict=strict)
        mapping = mappings.get(panel_id)
        out.append(
            {
                "panel_id": panel.id,
                "panel_name": panel.name,
                "source_panel_id": str(panel.source_panel_id),
                "groups": [
                    {
                        "source_group_id": gid,
                        "name": inventory.names.get(gid, gid),
                        "eligible": gid in inventory.eligible_set,
                        "selected_for_user": bool(stored and gid in stored),
                    }
                    for gid in sorted(inventory.names, key=lambda g: (len(g), g))
                ],
                "explicit_group_ids": stored,
                "binding_id": binding.id if binding is not None else None,
                "selection_required": decision.blocked,
                "selection_issue": decision.issue,
                "mapping_status": mapping.status if mapping is not None else None,
                "import_selected_group_ids": list(inventory.eligible_ids),
                "selection_message": decision.message,
            }
        )
    return out


async def build_user_seller_group_panels(
    db: AsyncSession,
    db_user: Any,
    *,
    panel_visible: Any = None,
) -> list[dict[str, Any]]:
    """Seller-group panels for an existing user (reachable through the user's enabled local groups)."""
    from app.node.user import _inbounds_from_loaded_groups

    inbounds = _inbounds_from_loaded_groups(db_user)
    if inbounds is None:
        inbounds = await db_user.inbounds()
    return await build_seller_group_panels(db, list(inbounds), user_id=db_user.id, panel_visible=panel_visible)


async def inbound_tags_for_local_groups(db: AsyncSession, group_ids: list[int]) -> list[str]:
    """Inbound tags of enabled local groups."""
    from sqlalchemy.orm import selectinload

    from app.db.models import Group

    if not group_ids:
        return []
    groups = (
        await db.execute(
            select(Group).options(selectinload(Group.inbounds)).where(Group.id.in_(group_ids), Group.is_disabled.is_(False))
        )
    ).scalars().all()
    return sorted({inbound.tag for group in groups for inbound in group.inbounds})
