"""Seller groups of OC users come from the panel import selection (``OCPanelGroup.is_selected``).

Context (panel 6781 / seller panel 1811): the seller account of a user whose local group held the
``US (ai)`` destination was created with ``groups: []`` and received only the seller default group
(Core). The admin selects the seller groups once, during the panel import; every user provisioned on
that panel must then receive exactly those source group ids. Nothing is asked per user and nothing is
derived from the user's configs.

Two layers are covered: pure decisions, and the real ``enqueue_oc_user_sync`` -> ``process_oc_sync``
path against the (throwaway) test database with a fake OC client.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details, get_admin_by_id
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import Admin, AdminRole, Group, ProxyInbound, Tenant, TenantStatus, User, UserStatus
from app.db.models_oc import (
    OCIntegration,
    OCPanel,
    OCPanelDestinationHost,
    OCPanelGroup,
    OCSyncState,
    OCUserMapping,
    UserPanelBinding,
)
from app.jobs.process_oc_sync import build_oc_user_put_bodies, process_oc_sync
from app.models.user_panel_binding import UserPanelBindingCreate, UserPanelBindingUpdate
from app.node.oc_sync import build_oc_user_put_payload, enqueue_oc_user_sync, legacy_panel_groups_fallback_is_allowed
from app.operation import OperatorType
from app.operation.user_panel_binding import UserPanelBindingOperation
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_seller_groups import (
    ISSUE_IMPORT_SELECTION_MISSING,
    OcSellerGroupSelectionError,
    PanelGroupInventory,
    assert_job_groups_still_eligible,
    decide_import_groups,
    group_drift,
    load_panel_group_inventory,
    mark_panel_group_assignments_stale,
)
from app.services.oc_share_link import destination_virtual_inbound_tag

SELLER_GROUPS = [("1", "Core"), ("2", "Operators"), ("5", "Tunnel"), ("6", "Gaming")]
US_LINK = "vless://u@us.example.test:639?type=xhttp&encryption=none#US%20(ai)"
CORE_LINK = "vless://u@fi.example.test:444?type=xhttp&encryption=none#FI"
SUB_URL = "https://sub.example.test/x"


def _inventory(selected: list[str], imported: list[str] | None = None) -> PanelGroupInventory:
    imported = imported if imported is not None else [g for g, _ in SELLER_GROUPS]
    names = {gid: dict(SELLER_GROUPS).get(gid, gid) for gid in imported}
    return PanelGroupInventory(panel_id=1, names=names, eligible_ids=tuple(sorted(selected)))


# --------------------------------------------------------------------------- pure decisions


def test_import_selection_core_and_operators_yields_exactly_those_groups():
    decision = decide_import_groups(_inventory(["1", "2"]), strict=True)
    assert not decision.blocked
    assert decision.groups == ("1", "2")


def test_unselected_groups_are_never_assigned():
    decision = decide_import_groups(_inventory(["1", "2"]), strict=True)
    assert set(decision.groups).isdisjoint({"5", "6"})  # Tunnel / Gaming were imported but not selected


def test_missing_import_selection_is_blocked_with_actionable_message():
    decision = decide_import_groups(_inventory([]), strict=True)
    assert decision.issue == ISSUE_IMPORT_SELECTION_MISSING
    assert decision.groups == ()  # never first group, default group or all groups
    assert "Operators [2]" in decision.message and "panel import" in decision.message
    assert "not created or updated" in decision.message


def test_panel_without_any_imported_group_rows_keeps_the_legacy_payload():
    # nothing was ever imported: there is no selection to apply and nothing to guess
    decision = decide_import_groups(_inventory([], imported=[]), strict=True)
    assert not decision.blocked
    assert decision.groups == ()


def test_kill_switch_only_relaxes_the_missing_selection_case():
    assert not decide_import_groups(_inventory([]), strict=False).blocked
    assert decide_import_groups(_inventory([]), strict=False).groups == ()
    assert decide_import_groups(_inventory(["2"]), strict=False).groups == ("2",)  # selection still wins


def test_legacy_all_groups_fallback_remains_disabled():
    assert legacy_panel_groups_fallback_is_allowed() is False


def test_group_drift_semantics():
    assert group_drift(None, ("1", "2")) is False  # unknown (legacy account): never an automatic bulk resync
    assert group_drift(["2", "1"], ("1", "2")) is False  # order-insensitive
    assert group_drift(["1", "2", "5"], ("1", "2")) is True  # a removed group must be reconciled
    assert group_drift(["1"], ("1", "2")) is True  # an added group must be reconciled
    assert group_drift([], ("1",)) is True


@pytest.mark.asyncio
async def test_payload_builder_uses_import_groups_verbatim_and_never_derives_from_configs():
    with patch("app.node.oc_sync.desired_oc_group_ids", new_callable=AsyncMock, return_value=["6"]) as derived:
        payload = await build_oc_user_put_payload(None, 1, ["cfg-a"], import_group_ids=["2", "1"])
    assert payload == {"groups": ["1", "2"], "configs": ["cfg-a"]}
    derived.assert_not_called()  # the user's configs cannot widen (or narrow) the seller groups


@pytest.mark.asyncio
async def test_payload_builder_default_contract_is_unchanged():
    with patch("app.node.oc_sync.desired_oc_group_ids", new_callable=AsyncMock, return_value=[]), patch(
        "app.node.oc_sync.selected_oc_panel_group_ids", new_callable=AsyncMock
    ) as all_groups:
        payload = await build_oc_user_put_payload(None, 1, ["cfg-a"])
    assert payload == {"groups": [], "configs": ["cfg-a"]}
    all_groups.assert_not_called()


def test_seller_put_bodies_carry_the_import_groups():
    assert build_oc_user_put_bodies(["1", "2"], ["c"]) == [
        {"groups": ["1", "2"], "configs": []},
        {"groups": ["1", "2"], "configs": ["c"]},
    ]
    assert build_oc_user_put_bodies([], ["cfg"]) == [{"groups": [], "configs": ["cfg"]}]


# --------------------------------------------------------------------------- database helpers


class _World:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def _make_world(
    db,
    *,
    groups: list[tuple[str, str]] = SELLER_GROUPS,
    selected: set[str] | None = None,
    links: list[str] | None = None,
) -> _World:
    selected = {"1", "2"} if selected is None else selected  # the admin imported Core + Operators
    tenant = Tenant(name=f"ig_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    admin = Admin(username=f"igadm_{uuid.uuid4().hex[:8]}", hashed_password="x", role_id=role.id, tenant_id=tenant.id)
    db.add(admin)
    await db.flush()
    await create_workspace_for_administrator(db, admin)
    ws = admin.workspace_id
    intg = OCIntegration(base_url="https://oc.example.test", api_token_encrypted="e", token_preview="t")
    db.add(intg)
    await db.flush()
    panel = OCPanel(
        integration_id=intg.id,
        source_panel_id="1811",
        purchaser_identity="buyer",
        name="Panel 3 (server3)",
        tenant_id=tenant.id,
        workspace_id=ws,
        sync_status="connected",
    )
    db.add(panel)
    await db.flush()
    for gid, name in groups:
        db.add(OCPanelGroup(panel_id=panel.id, source_group_id=gid, source_name=name, is_selected=gid in selected))
    await reconcile_panel_destination_hosts(db, panel, links or [US_LINK])
    await db.flush()
    dests = (
        await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
    ).scalars().all()
    tags = [destination_virtual_inbound_tag(panel.id, d.destination_config_id) for d in dests]
    inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(tags)))).scalars().all()
    group = Group(name=f"local_{uuid.uuid4().hex[:6]}", inbounds=list(inbounds), tenant_id=tenant.id, workspace_id=ws)
    db.add(group)
    await db.flush()
    user = User(
        username=f"igu_{uuid.uuid4().hex[:6]}",
        status=UserStatus.active,
        admin_id=admin.id,
        workspace_id=ws,
        proxy_settings={"vless": {"id": str(uuid.uuid4())}},
    )
    user.groups = [group]
    db.add(user)
    await db.flush()
    return _World(
        tenant=tenant,
        admin=admin,
        panel=panel,
        user=user,
        group=group,
        dests=dests,
        inbounds=list(inbounds),
        tags=tags,
        ws=ws,
        dest_by_name={d.display_name: d for d in dests},
        inbound_by_tag={i.tag: i for i in inbounds},
    )


async def _enqueue(db, user, **kw):
    with patch("app.node.oc_sync.tenant_panel_allows_oc_user_mutations", new_callable=AsyncMock, return_value=True):
        await enqueue_oc_user_sync(db, user, **kw)
    await db.flush()


async def _jobs(db, user, panel):
    return (
        await db.execute(
            select(OCSyncState).where(
                OCSyncState.entity_type == "user_mapping", OCSyncState.entity_id == f"{user.id}_{panel.id}"
            )
        )
    ).scalars().all()


async def _mapping(db, user, panel):
    return (
        await db.execute(
            select(OCUserMapping).where(OCUserMapping.user_id == user.id, OCUserMapping.panel_id == panel.id)
        )
    ).scalar_one_or_none()


async def _set_selection(db, panel, selected: set[str]):
    for row in (await db.execute(select(OCPanelGroup).where(OCPanelGroup.panel_id == panel.id))).scalars():
        row.is_selected = row.source_group_id in selected
    await db.flush()


def _active_mapping(w, *, configs: list[str], groups: list[str] | None):
    return OCUserMapping(
        user_id=w.user.id,
        panel_id=w.panel.id,
        external_user_id=str(uuid.uuid4()),
        status="active",
        last_synced_configs=list(configs),
        last_synced_at=datetime.now(UTC),
        external_subscription_url=SUB_URL,
        last_synced_group_ids=groups,
    )


# --------------------------------------------------------------------------- creation


@pytest.mark.asyncio
async def test_new_user_receives_exactly_the_import_selected_groups_with_no_per_user_step():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        assert (await db.execute(select(UserPanelBinding).where(UserPanelBinding.user_id == w.user.id))).first() is None

        await _enqueue(db, w.user)  # plain user creation, no seller-group interaction of any kind

        (job,) = await _jobs(db, w.user, w.panel)
        assert job.operation == "create" and job.status == "pending"
        payload = job.payload
        assert payload["groups"] == ["1", "2"]  # Core + Operators (the production bug: this used to be [])
        assert not {"5", "6"} & set(payload["groups"])  # Tunnel / Gaming are never added
        assert payload["group_selection"] == {"source": "panel_import", "groups": ["1", "2"]}
        us_key = w.dests[0].destination_config_id
        assert payload["sync_keys"] == [us_key]  # the US destination is not lost
        assert build_oc_user_put_bodies(payload["groups"], payload["configs"])[0]["groups"] == ["1", "2"]
        assert (await _mapping(db, w.user, w.panel)).last_synced_group_ids is None  # recorded only after a PUT
        await db.rollback()


@pytest.mark.asyncio
async def test_single_selected_group_panel_assigns_that_group():
    async with GetDB() as db:
        w = await _make_world(db, selected={"2"})
        await _enqueue(db, w.user)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.payload["groups"] == ["2"]
        await db.rollback()


@pytest.mark.asyncio
async def test_missing_import_selection_creates_no_account_and_is_diagnosable(caplog):
    async with GetDB() as db:
        w = await _make_world(db, selected=set())
        caplog.clear()
        with caplog.at_level("WARNING"):
            await _enqueue(db, w.user)
        assert await _jobs(db, w.user, w.panel) == []
        assert await _mapping(db, w.user, w.panel) is None  # not "apparently synchronized"
        # the diagnostic names the panel, the issue and the remedy (it may already have been rate-limited
        # by an earlier test in the same process, so only assert on it when emitted)
        messages = " ".join(r.getMessage() for r in caplog.records)
        if "oc_seller_group_selection_blocked" in messages:
            assert f"panel_id={w.panel.id}" in messages and "import_selection_missing" in messages
        await db.rollback()


@pytest.mark.asyncio
async def test_panel_without_imported_group_rows_keeps_legacy_empty_groups_never_all_groups():
    async with GetDB() as db:
        w = await _make_world(db, groups=[], selected=set())
        await _enqueue(db, w.user)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.payload["groups"] == []  # unchanged legacy behaviour for a never-group-imported panel
        assert "group_selection" not in job.payload  # no import selection was applied or recorded
        await db.rollback()


@pytest.mark.asyncio
async def test_legacy_panel_never_derives_groups_from_catalog_group_mapping():
    """No group rows -> no selection to intersect with: the catalog's group_mapping must not grant anything."""
    from app.db.models_oc import OCPanelConfig

    async with GetDB() as db:
        w = await _make_world(db, groups=[], selected=set())
        db.add(
            OCPanelConfig(
                panel_id=w.panel.id,
                source_config_id="cfg-1",
                source_name="US",
                source_payload={"group_mapping": {"supported": True, "groups": ["1", "2", "5", "6"]}},
            )
        )
        await db.flush()
        payload = await build_oc_user_put_payload(db, w.panel.id, ["cfg-1"])
        assert payload["groups"] == []  # never widened by the catalog or by the user's config choice
        await db.rollback()


@pytest.mark.asyncio
async def test_panel_leaves_the_legacy_path_as_soon_as_groups_are_imported_even_if_none_selected():
    """An imported panel cannot slip through the legacy branch: once group rows exist it is blocked or exact."""
    async with GetDB() as db:
        w = await _make_world(db, groups=[], selected=set())
        legacy = decide_import_groups(await load_panel_group_inventory(db, w.panel.id))
        assert not legacy.blocked and legacy.groups == ()  # the only legacy-allowed shape: zero group rows

        # a panel refresh discovers the seller's groups (created unselected, never auto-selected)
        for gid, name in SELLER_GROUPS:
            db.add(OCPanelGroup(panel_id=w.panel.id, source_group_id=gid, source_name=name, is_selected=False))
        await db.flush()
        after = decide_import_groups(await load_panel_group_inventory(db, w.panel.id))
        assert after.blocked and after.issue == ISSUE_IMPORT_SELECTION_MISSING
        await _enqueue(db, w.user)
        assert await _jobs(db, w.user, w.panel) == []  # nothing is sent to the seller

        await _set_selection(db, w.panel, {"1", "2"})
        await _enqueue(db, w.user)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.payload["groups"] == ["1", "2"]
        await db.rollback()


@pytest.mark.asyncio
async def test_deprecated_per_user_value_is_ignored_by_provisioning():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        db.add(UserPanelBinding(tenant_id=w.tenant.id, user_id=w.user.id, oc_panel_id=w.panel.id,
                                desired_source_group_ids=["5"]))
        await db.flush()
        await _enqueue(db, w.user)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.payload["groups"] == ["1", "2"]  # neither replaced nor united with the per-user value
        await db.rollback()


# --------------------------------------------------------------------------- config edits do not change groups


@pytest.mark.asyncio
async def test_editing_selected_configs_does_not_change_seller_groups():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"}, links=[US_LINK, CORE_LINK])
        us, fi = w.dest_by_name["US (ai)"], w.dest_by_name["FI"]
        us_inb = w.inbound_by_tag[destination_virtual_inbound_tag(w.panel.id, us.destination_config_id)]
        fi_inb = w.inbound_by_tag[destination_virtual_inbound_tag(w.panel.id, fi.destination_config_id)]
        w.group.inbounds = [us_inb]
        await db.flush()
        db.add(_active_mapping(w, configs=[us.destination_config_id], groups=["1", "2"]))
        await db.flush()

        await _enqueue(db, w.user)
        assert await _jobs(db, w.user, w.panel) == []  # nothing changed -> no job

        # add a host of an already selected group: configs change, groups must not
        w.group.inbounds = [us_inb, fi_inb]
        await db.flush()
        await _enqueue(db, w.user)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.operation == "update"
        assert set(job.payload["sync_keys"]) == {us.destination_config_id, fi.destination_config_id}
        assert job.payload["groups"] == ["1", "2"]  # not expanded, not narrowed

        # remove a config again: still the import selection, never "groups of the remaining configs"
        w.group.inbounds = [fi_inb]
        await db.flush()
        await _enqueue(db, w.user)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.payload["sync_keys"] == [fi.destination_config_id]
        assert job.payload["groups"] == ["1", "2"]
        await db.rollback()


# --------------------------------------------------------------------------- changed import selection


@pytest.mark.asyncio
async def test_changed_import_selection_applies_to_future_creation_without_duplicate_jobs():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        await _enqueue(db, w.user)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.payload["groups"] == ["1", "2"]

        await _set_selection(db, w.panel, {"1", "5"})  # admin edits the import selection
        await _enqueue(db, w.user)  # the account was not created yet: the pending job is refreshed in place
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.payload["groups"] == ["1", "5"]
        assert "2" not in job.payload["groups"]
        await db.rollback()


@pytest.mark.asyncio
async def test_removed_group_is_reconciled_for_existing_accounts_exactly_once():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        key = w.dests[0].destination_config_id
        db.add(_active_mapping(w, configs=[key], groups=["1", "2"]))
        await db.flush()
        await _enqueue(db, w.user)
        assert await _jobs(db, w.user, w.panel) == []  # converged: nothing to do

        await _set_selection(db, w.panel, {"1"})  # Operators removed from the import selection
        await _enqueue(db, w.user, periodic_reconcile=True)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.operation == "update" and job.payload["groups"] == ["1"]
        assert job.payload["sync_keys"] == [key]  # configs untouched

        await _enqueue(db, w.user, periodic_reconcile=True)  # next tick while the job is still pending
        assert len(await _jobs(db, w.user, w.panel)) == 1  # idempotent: no duplicate / conflicting job
        await db.rollback()


@pytest.mark.asyncio
async def test_added_group_is_reconciled_for_existing_accounts():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1"})
        key = w.dests[0].destination_config_id
        db.add(_active_mapping(w, configs=[key], groups=["1"]))
        await db.flush()
        await _set_selection(db, w.panel, {"1", "2"})
        await _enqueue(db, w.user, periodic_reconcile=True)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.payload["groups"] == ["1", "2"]
        await db.rollback()


@pytest.mark.asyncio
async def test_unknown_applied_groups_are_not_bulk_resynced_until_the_admin_saves_the_selection():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        key = w.dests[0].destination_config_id
        db.add(_active_mapping(w, configs=[key], groups=None))  # account created before group tracking
        await db.flush()

        await _enqueue(db, w.user, periodic_reconcile=True)
        assert await _jobs(db, w.user, w.panel) == []  # deploying alone never triggers a surprise resync

        assert await mark_panel_group_assignments_stale(db, w.panel.id) == 1  # admin saved the import selection
        assert await mark_panel_group_assignments_stale(db, w.panel.id) == 0  # idempotent
        await _enqueue(db, w.user, periodic_reconcile=True)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.payload["groups"] == ["1", "2"]
        await db.rollback()


@pytest.mark.asyncio
async def test_emptied_import_selection_never_widens_and_keeps_the_account_state():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        key = w.dests[0].destination_config_id
        db.add(_active_mapping(w, configs=[key], groups=["1", "2"]))
        await db.flush()
        await _set_selection(db, w.panel, set())
        await _enqueue(db, w.user, periodic_reconcile=True)
        assert await _jobs(db, w.user, w.panel) == []  # blocked: nothing is sent, no grant of any group
        mapping = await _mapping(db, w.user, w.panel)
        assert mapping.last_synced_group_ids == ["1", "2"]  # not falsely rewritten
        await db.rollback()


@pytest.mark.asyncio
async def test_worker_revalidation_rejects_groups_no_longer_selected():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        await assert_job_groups_still_eligible(db, w.panel.id, ["1", "2"])
        await assert_job_groups_still_eligible(db, w.panel.id, [])
        with pytest.raises(OcSellerGroupSelectionError) as exc:
            await assert_job_groups_still_eligible(db, w.panel.id, ["2", "6"])
        assert "6" in str(exc.value) and "no seller request was sent" in str(exc.value)
        await db.rollback()


# --------------------------------------------------------------------------- real worker path (fake OC)


class _NoCommit:
    """Proxy so the worker's ``commit()`` only flushes: the test world is rolled back at the end."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def commit(self):
        await self._db.flush()


class _GetDBCtx:
    def __init__(self, db):
        self._proxy = _NoCommit(db)

    async def __aenter__(self):
        return self._proxy

    async def __aexit__(self, *exc):
        return False


class _FakeResp:
    def __init__(self, status: int):
        self.status = status

    async def json(self):
        return {"subscription_url": SUB_URL} if self.status < 400 else {"detail": "boom"}

    async def text(self):
        return json.dumps(await self.json())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_client(status_box: dict, puts: list):
    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def put(self, url, headers=None, json=None):
            puts.append(json)
            return _FakeResp(status_box["status"])

        def delete(self, url, headers=None):
            return _FakeResp(200)

    return _Session


async def _run_worker(db, status_box, puts):
    with patch("app.jobs.process_oc_sync.GetDB", new=lambda: _GetDBCtx(db)), patch(
        "app.jobs.process_oc_sync.aiohttp.ClientSession", new=_fake_client(status_box, puts)
    ), patch("app.jobs.process_oc_sync.decrypt_secret", new=AsyncMock(return_value="tok")), patch(
        "app.jobs.process_oc_sync.connection_token_for_panel", new=AsyncMock(return_value=None)
    ), patch("app.jobs.process_oc_sync.panel_has_active_connection", new=AsyncMock(return_value=True)):
        await process_oc_sync()


@pytest.mark.asyncio
async def test_worker_creates_account_with_import_groups_and_records_them_only_on_success():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        await _enqueue(db, w.user)
        (job,) = await _jobs(db, w.user, w.panel)

        # 1) seller unavailable: nothing may be marked synchronized
        puts: list = []
        await _run_worker(db, {"status": 503}, puts)
        await db.refresh(job)
        mapping = await _mapping(db, w.user, w.panel)
        assert job.attempts == 1 and job.status == "pending" and job.last_error
        assert mapping.last_synced_group_ids is None and mapping.last_synced_configs is None
        assert mapping.status != "active"
        assert all(body["groups"] == ["1", "2"] for body in puts)

        # 2) retry succeeds: exactly the import groups were sent and are now recorded
        puts = []
        await _run_worker(db, {"status": 200}, puts)
        await db.refresh(job)
        await db.refresh(mapping)
        assert job.status == "completed"
        assert puts and all(body["groups"] == ["1", "2"] for body in puts)
        assert not any({"5", "6"} & set(body["groups"]) for body in puts)
        assert mapping.last_synced_group_ids == ["1", "2"] and mapping.status == "active"
        await db.rollback()


@pytest.mark.asyncio
async def test_removed_group_is_pushed_retried_and_only_then_recorded():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        key = w.dests[0].destination_config_id
        mapping = _active_mapping(w, configs=[key], groups=["1", "2"])
        db.add(mapping)
        await db.flush()
        await _set_selection(db, w.panel, {"1"})  # Operators removed

        await _enqueue(db, w.user, periodic_reconcile=True)
        (job,) = await _jobs(db, w.user, w.panel)

        # every attempt fails until the job is exhausted: the removal is NOT recorded as applied
        for attempt in range(1, job.max_attempts + 1):
            puts: list = []
            await _run_worker(db, {"status": 503}, puts)
            await db.refresh(job)
            assert job.attempts == attempt
            assert puts and all(body["groups"] == ["1"] for body in puts)
        assert job.status == "failed"
        await db.refresh(mapping)
        assert mapping.last_synced_group_ids == ["1", "2"]  # still reflects what the seller really has

        # the drift is still visible, so the next reconcile queues a fresh job (safe retry)
        await _enqueue(db, w.user, periodic_reconcile=True)
        pending = [j for j in await _jobs(db, w.user, w.panel) if j.status == "pending"]
        assert len(pending) == 1 and pending[0].payload["groups"] == ["1"]

        puts = []
        await _run_worker(db, {"status": 200}, puts)
        await db.refresh(mapping)
        assert puts and all(body["groups"] == ["1"] for body in puts)
        assert mapping.last_synced_group_ids == ["1"]  # recorded only after success

        # converged: no further job
        await _enqueue(db, w.user, periodic_reconcile=True)
        assert [j for j in await _jobs(db, w.user, w.panel) if j.status == "pending"] == []
        await db.rollback()


# --------------------------------------------------------------------------- import selection edit endpoint


class _NoCommitDb(_NoCommit):
    pass


@pytest.mark.asyncio
async def test_saving_the_import_selection_flags_unknown_accounts_and_updates_selection():
    from app.routers import integration as integration_router
    from app.routers.integration import SyncRequest, sync_configs_and_hosts

    async def fake_oc(db, panel, method, path, json=None):
        assert method == "GET"
        if path.endswith("/groups"):
            return {"groups": [{"id": gid, "name": name} for gid, name in SELLER_GROUPS]}
        if path.endswith("/configs"):
            return {"configs": []}
        return {}

    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        key = w.dests[0].destination_config_id
        legacy = _active_mapping(w, configs=[key], groups=None)
        db.add(legacy)
        await db.flush()

        with patch.object(integration_router, "_require_tenant_admin", return_value=object()), patch.object(
            integration_router, "ensure_actor_panel_access", new=AsyncMock()
        ), patch.object(
            integration_router, "require_active_telegram_for_tenant_admin", new=AsyncMock()
        ), patch.object(integration_router, "_oc_panel_call", new=fake_oc):
            resp = await sync_configs_and_hosts(
                w.panel.id,
                SyncRequest(selected_group_ids=["1", "5"], group_names={}),
                _NoCommitDb(db),
                ("owner", True, None),
            )
        assert resp.status in ("success", "partial")

        inventory = await load_panel_group_inventory(db, w.panel.id)
        assert inventory.eligible_ids == ("1", "5")  # persisted selection updated
        await db.refresh(legacy)
        assert legacy.last_synced_group_ids == []  # flagged: the reconcile queues the update

        await _enqueue(db, w.user, periodic_reconcile=True)
        (job,) = await _jobs(db, w.user, w.panel)
        assert job.payload["groups"] == ["1", "5"]  # Operators removed, Tunnel added
        await db.rollback()


async def _call_import_selection(db, w, selected, *, oc_calls):
    from app.routers import integration as integration_router
    from app.routers.integration import SyncRequest, sync_configs_and_hosts

    async def fake_oc(db_, panel, method, path, json=None):
        oc_calls.append((method, path))
        if path.endswith("/groups"):
            return {"groups": [{"id": gid, "name": name} for gid, name in SELLER_GROUPS]}
        if path.endswith("/configs"):
            return {"configs": []}
        return {}

    with patch.object(integration_router, "_require_tenant_admin", return_value=object()), patch.object(
        integration_router, "ensure_actor_panel_access", new=AsyncMock()
    ), patch.object(integration_router, "require_active_telegram_for_tenant_admin", new=AsyncMock()), patch.object(
        integration_router, "_oc_panel_call", new=fake_oc
    ):
        return await sync_configs_and_hosts(
            w.panel.id, SyncRequest(selected_group_ids=selected, group_names={}), _NoCommitDb(db), ("owner", True, None)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_selection", [[], [""], ["  "]])
async def test_import_selection_endpoint_rejects_an_empty_selection_server_side(bad_selection):
    from fastapi import HTTPException

    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        legacy = _active_mapping(w, configs=[w.dests[0].destination_config_id], groups=["1", "2"])
        db.add(legacy)
        await db.flush()
        oc_calls: list = []
        with pytest.raises(HTTPException) as exc:
            await _call_import_selection(db, w, bad_selection, oc_calls=oc_calls)
        assert exc.value.status_code == 400
        assert "at least one" in exc.value.detail.lower()
        assert oc_calls == []  # rejected before anything is read from the seller

        # nothing was written: the existing selection and the account's recorded groups are untouched
        inventory = await load_panel_group_inventory(db, w.panel.id)
        assert inventory.eligible_ids == ("1", "2")
        await db.refresh(legacy)
        assert legacy.last_synced_group_ids == ["1", "2"]
        await db.rollback()


@pytest.mark.asyncio
async def test_import_selection_endpoint_rejects_groups_the_seller_panel_does_not_have():
    from fastapi import HTTPException

    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        oc_calls: list = []
        with pytest.raises(HTTPException) as exc:
            await _call_import_selection(db, w, ["1", "999"], oc_calls=oc_calls)
        assert exc.value.status_code == 400
        assert "999" in exc.value.detail
        inventory = await load_panel_group_inventory(db, w.panel.id)
        assert inventory.eligible_ids == ("1", "2")  # a partly-invalid request changes nothing
        await db.rollback()


@pytest.mark.asyncio
async def test_import_selection_endpoint_persists_exactly_the_valid_selection():
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        oc_calls: list = []
        resp = await _call_import_selection(db, w, ["5"], oc_calls=oc_calls)
        assert resp.status in ("success", "partial")
        inventory = await load_panel_group_inventory(db, w.panel.id)
        assert inventory.eligible_ids == ("5",)  # only Tunnel selected; Core/Operators/Gaming are not
        assert set(inventory.names) >= {"1", "2", "5"}
        await db.rollback()


# --------------------------------------------------------------------------- imported != eligible (resync keeps it)


@pytest.mark.asyncio
async def test_panel_resync_keeps_the_import_selection_and_new_groups_are_not_selected():
    from app.node.oc_sync import sync_panel_from_outbound_center

    async def fake_oc(integration, method, path, json=None, *, connection_token=None):
        assert method == "GET"  # a refresh never writes to the seller
        if path.endswith("/groups"):
            return {
                "groups": [
                    {"id": "1", "name": "Core"},
                    {"id": "2", "name": "Operators (renamed)"},
                    {"id": "5", "name": "Tunnel"},
                    {"id": "7", "name": "Brand New"},
                ]
            }
        if path.endswith("/configs"):
            return {"configs": []}
        return {"status": "ACTIVE"}

    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        with patch("app.services.oc_integration_client.call_oc_api", new=fake_oc), patch(
            "app.services.oc_connection_credentials.connection_token_for_panel",
            new=AsyncMock(return_value="tok"),
        ):
            await sync_panel_from_outbound_center(db, w.panel.id)
        await db.flush()
        after = await load_panel_group_inventory(db, w.panel.id)
        assert after.eligible_ids == ("1", "2")
        assert after.names["2"] == "Operators (renamed)"
        assert "7" in after.names and "7" not in after.eligible_ids  # discovered, never granted
        await db.rollback()


# --------------------------------------------------------------------------- deprecated API contract kept


async def _tenant_admin_details(db, tenant_id: int):
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    admin = Admin(username=f"igop_{uuid.uuid4().hex[:8]}", hashed_password="x", role_id=role.id, tenant_id=tenant_id)
    db.add(admin)
    await db.flush()
    await create_workspace_for_administrator(db, admin)
    return admin, build_admin_details(await get_admin_by_id(db, admin.id, load_role=True))


@pytest.mark.asyncio
async def test_deprecated_binding_field_keeps_its_contract_but_has_no_sync_side_effect():
    op = UserPanelBindingOperation(OperatorType.API)
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        details = build_admin_details(await get_admin_by_id(db, w.admin.id, load_role=True))

        with pytest.raises(HTTPException) as bad:  # validation contract unchanged: 400 + available groups
            await op.create_binding(
                db, w.user.id, UserPanelBindingCreate(oc_panel_id=w.panel.id, desired_source_group_ids=["2", "42"]), details
            )
        assert bad.value.status_code == 400
        assert "42" in str(bad.value.detail) and "Operators [2]" in str(bad.value.detail)

        created = await op.create_binding(
            db, w.user.id, UserPanelBindingCreate(oc_panel_id=w.panel.id, desired_source_group_ids=["2"]), details
        )
        assert created.desired_source_group_ids == ["2"]  # still stored and returned
        updated = await op.update_binding(
            db, w.user.id, created.id, UserPanelBindingUpdate(desired_source_group_ids=["1"]), details
        )
        assert updated.desired_source_group_ids == ["1"]
        with pytest.raises(HTTPException) as bad2:
            await op.update_binding(
                db, w.user.id, created.id, UserPanelBindingUpdate(desired_source_group_ids=["6"]), details
            )
        assert bad2.value.status_code == 400  # Gaming was not selected during import
        await db.flush()
        assert await _jobs(db, w.user, w.panel) == []  # no per-user resync any more
        await db.rollback()


@pytest.mark.asyncio
async def test_read_endpoints_report_the_import_selection_and_missing_selection_diagnostic():
    op = UserPanelBindingOperation(OperatorType.API)
    async with GetDB() as db:
        w = await _make_world(db, selected={"1", "2"})
        details = build_admin_details(await get_admin_by_id(db, w.admin.id, load_role=True))
        (panel_opts,) = (await op.list_seller_groups(db, w.user.id, details)).panels
        assert panel_opts.import_selected_group_ids == ["1", "2"]
        assert panel_opts.selection_required is False and panel_opts.selection_message is None
        assert [(g.source_group_id, g.eligible) for g in panel_opts.groups] == [
            ("1", True), ("2", True), ("5", False), ("6", False),
        ]

        await _set_selection(db, w.panel, set())
        (panel_opts,) = (await op.list_seller_groups(db, w.user.id, details)).panels
        assert panel_opts.selection_required is True
        assert panel_opts.selection_issue == ISSUE_IMPORT_SELECTION_MISSING
        assert "panel import" in panel_opts.selection_message
        assert panel_opts.import_selected_group_ids == []
        await db.rollback()


@pytest.mark.asyncio
async def test_read_endpoints_do_not_leak_other_tenants_panels():
    op = UserPanelBindingOperation(OperatorType.API)
    async with GetDB() as db:
        w = await _make_world(db)
        other = Tenant(name=f"ig_other_{uuid.uuid4().hex[:6]}", status=TenantStatus.active)
        db.add(other)
        await db.flush()
        _, other_details = await _tenant_admin_details(db, other.id)
        options = await op.seller_group_options_for_local_groups(db, [g.id for g in w.user.groups], other_details)
        assert options.panels == []
        with pytest.raises(HTTPException) as exc:
            await op.create_binding(
                db, w.user.id, UserPanelBindingCreate(oc_panel_id=w.panel.id, desired_source_group_ids=["2"]), other_details
            )
        assert exc.value.status_code in (403, 404)
        await db.rollback()


def test_existing_api_contract_is_unchanged_and_only_extended():
    from fastapi import FastAPI

    from app.models.user_panel_binding import UserPanelBindingResponse, UserSellerGroupPanel
    from app.routers import api_router

    app = FastAPI()
    app.include_router(api_router)
    routes = {(path, method.upper()) for path, item in app.openapi()["paths"].items() for method in item}
    for method, path in [
        ("GET", "/api/users/{user_id}/panel-bindings"),
        ("POST", "/api/users/{user_id}/panel-bindings"),
        ("PATCH", "/api/users/{user_id}/panel-bindings/{binding_id}"),
        ("DELETE", "/api/users/{user_id}/panel-bindings/{binding_id}"),
        ("GET", "/api/users/{user_id}/oc-seller-groups"),  # shipped before this change: kept
        ("GET", "/api/oc-seller-groups"),
        ("POST", "/api/integration/panels/{panel_id}/sync"),
    ]:
        assert (path, method) in routes

    assert {
        "id", "tenant_id", "user_id", "oc_panel_id", "enabled", "source", "desired_config_ids",
        "desired_source_group_ids", "created_at", "updated_at",
    } == set(UserPanelBindingResponse.model_fields)
    shipped = {
        "panel_id", "panel_name", "source_panel_id", "groups", "explicit_group_ids", "binding_id",
        "selection_required", "selection_issue", "mapping_status",
    }
    assert shipped <= set(UserSellerGroupPanel.model_fields)
    assert set(UserSellerGroupPanel.model_fields) - shipped == {"import_selected_group_ids", "selection_message"}

    body = UserPanelBindingCreate.model_validate({"oc_panel_id": 5, "desired_config_ids": ["a"]})
    assert body.desired_source_group_ids is None
    for bad in ([], [""]):
        with pytest.raises(ValidationError):
            UserPanelBindingCreate(oc_panel_id=1, desired_source_group_ids=bad)
