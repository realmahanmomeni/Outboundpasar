"""End-to-end (isolated SQLite, synthetic data) regression tests for the OC subscription pipeline.

Covers: identity mapping across panels, failure-vs-empty semantics, bounded stale-if-error,
single-flight upstream fetches, deterministic output and cache behaviour.  No network, no
production data; every host is ``*.example`` and every credential is derived from a label.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.models import Group, ProxyInbound, User, UserStatus
from app.db.models_oc import OCUserMapping
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.services import oc_upstream_links_cache as cache
from app.services.oc_share_link import destination_virtual_inbound_tag
from app.services.oc_user_subscription_hosts import (
    build_oc_subscription_links_from_state,
    collect_oc_subscription_links_for_user,
    load_oc_subscription_links_state,
)
from app.subscription.share import generate_subscription
from tests import oc_link_fixtures as F

# Scaffolding (autouse fixtures + DB seed helpers) is shared with the group-selection suite.
from tests.test_oc_group_subscription_filter import (  # noqa: F401  (autouse fixtures must be imported)
    _active_connection,
    _clear_sub_cache,
    _empty_host_manager_cache,
    _isolated_sqlite_db,
    _minimal_client_templates,
    _panel,
    _tenant_admin,
)
from tests.oc_format_identity import fp, identities_for_format, link_identities

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _require_isolated_database(_isolated_sqlite_db):
    """Hard stop: these tests write rows, so they may only ever run on the private SQLite engine.

    Depends on ``_isolated_sqlite_db`` explicitly and re-checks the resolved bind, so a missing or
    shadowed fixture (e.g. a star-import, which skips underscore names) fails loudly instead of
    silently falling through to the application database.
    """
    from tests.pytest_db_guard import session_local_is_isolated

    assert session_local_is_isolated(), "refusing to write: SessionLocal is not the private SQLite engine"


# --------------------------------------------------------------------------------------------
# seeding helpers
# --------------------------------------------------------------------------------------------


@dataclass
class Seed:
    uid: int
    panels: list
    rows: list  # rows[i] = destination rows of panels[i]
    tags: list[str]  # selected tags, in (panel, row) order


async def _seed(
    discovery: list[list[str]],
    *,
    select_rows: dict[int, list[int]] | None = None,
    synced: dict[int, list[str]] | None = None,
) -> Seed:
    """One panel per entry of ``discovery`` (its destination hosts come from the discovery links).

    ``select_rows[i]`` limits which destination rows of panel *i* are in the user's group and
    ``synced[i]`` overrides ``last_synced_configs`` (default: exactly the selected rows).
    """
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panels, rows_by_panel, tags = [], [], []
        for i, links in enumerate(discovery):
            panel, rows = await _panel(db, tenant, admin, links, name=f"P{i}")
            rows = sorted(rows, key=lambda r: r.id)
            panels.append(panel)
            rows_by_panel.append(rows)
            chosen = (select_rows or {}).get(i, list(range(len(rows))))
            tags.extend(destination_virtual_inbound_tag(panel.id, rows[j].destination_config_id) for j in chosen)
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(tags)))).scalars().all()
        group = Group(name=f"g{uuid.uuid4().hex[:5]}", inbounds=list(inbounds), tenant_id=tenant.id,
                      workspace_id=admin.workspace_id)
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        for i, (panel, rows) in enumerate(zip(panels, rows_by_panel, strict=True)):
            chosen = (select_rows or {}).get(i, list(range(len(rows))))
            sync_ids = (synced or {}).get(i, [rows[j].destination_config_id for j in chosen])
            db.add(
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel.id,
                    external_user_id=f"ext{i}",
                    status="active",
                    last_synced_configs=list(sync_ids),
                    external_subscription_url=f"https://198.51.100.{i + 1}/sub/synthetic{i}",
                )
            )
        await db.commit()
        return Seed(uid=user.id, panels=panels, rows=rows_by_panel, tags=tags)


class Seller:
    """Fake seller panels keyed by panel id: a list of links, an exception, or a callable."""

    def __init__(self, behaviour: dict):
        self.behaviour = behaviour
        self.calls: list[int] = []

    async def __call__(self, mapping):
        self.calls.append(mapping.panel_id)
        item = self.behaviour[mapping.panel_id]
        if callable(item):
            item = await item()
        if isinstance(item, BaseException):
            raise item
        return list(item)


def _patched(seller: Seller):
    return patch("app.services.oc_user_subscription_hosts._fetch_user_links", new=seller)


async def _collect(seed: Seed, tags: list[str] | None = None) -> list[str]:
    async with GetDB() as db:
        return await collect_oc_subscription_links_for_user(db, seed.uid, tags or seed.tags, {}, user_tenant_id=None)


async def _generate(seed: Seed, fmt: str) -> str:
    sub_op = SubscriptionOperation(OperatorType.API)
    async with GetDB() as db:
        from sqlalchemy.orm import selectinload

        user = (
            await db.execute(
                select(User).where(User.id == seed.uid).options(selectinload(User.groups).selectinload(Group.inbounds))
            )
        ).scalar_one()
        validated = await sub_op.validated_user(user)
    return await generate_subscription(validated, fmt, False)


# --------------------------------------------------------------------------------------------
# identity mapping
# --------------------------------------------------------------------------------------------


async def test_same_host_port_different_credentials_on_two_panels_keep_their_own_credential():
    seed = await _seed([[F.vless("disc-1", "shared.example", 443, remark="X")],
                        [F.vless("disc-2", "shared.example", 443, remark="X")]])
    seller = Seller({
        seed.panels[0].id: [F.vless("cust-p1", "shared.example", 443, remark="X")],
        seed.panels[1].id: [F.vless("cust-p2", "shared.example", 443, remark="X")],
    })
    with _active_connection(), _patched(seller):
        links = await _collect(seed)
    got = link_identities(links)
    assert sorted(i[3] for i in got) == sorted([fp(F.cred("cust-p1")), fp(F.cred("cust-p2"))])
    assert fp(F.cred("disc-1")) not in {i[3] for i in got}, "discovery credential must never be served"
    assert fp(F.cred("disc-2")) not in {i[3] for i in got}


async def test_same_credential_from_two_panels_on_different_hosts_yields_two_entries():
    seed = await _seed([[F.vless("d1", "one.example", 443, remark="One")],
                        [F.vless("d2", "two.example", 443, remark="Two")]])
    seller = Seller({
        seed.panels[0].id: [F.vless("same-cred", "one.example", 443, remark="One")],
        seed.panels[1].id: [F.vless("same-cred", "two.example", 443, remark="Two")],
    })
    with _active_connection(), _patched(seller):
        got = link_identities(await _collect(seed))
    assert sorted((i[1], i[2]) for i in got) == [("one.example", 443), ("two.example", 443)]
    assert len({i[3] for i in got}) == 1


async def test_a_panel_never_receives_links_from_another_panels_pool():
    seed = await _seed([[F.vless("d1", "a.example", 443, remark="A")],
                        [F.vless("d2", "b.example", 443, remark="B")]])
    seller = Seller({
        seed.panels[0].id: [F.vless("c1", "a.example", 443, remark="A"), F.vless("leak", "b.example", 443, remark="B")],
        seed.panels[1].id: [F.vless("c2", "b.example", 443, remark="B")],
    })
    with _active_connection(), _patched(seller):
        got = link_identities(await _collect(seed))
    assert fp(F.cred("leak")) not in {i[3] for i in got}
    assert sorted(i[3] for i in got) == sorted([fp(F.cred("c1")), fp(F.cred("c2"))])


async def test_stale_or_unknown_sync_ids_do_not_widen_selection():
    seed = await _seed(
        [[F.vless("d0", "a.example", 443, remark="A"), F.vless("d1", "b.example", 443, remark="B"),
          F.vless("d2", "c.example", 443, remark="C")]],
        select_rows={0: [0, 1]},
        # one selected+synced row, an id that no longer exists, and one synced row NOT in the group
        synced={0: ["__placeholder__"]},
    )
    async with GetDB() as db:
        mapping = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == seed.uid))).scalar_one()
        mapping.last_synced_configs = [
            seed.rows[0][0].destination_config_id,
            "deleted-config-999",
            seed.rows[0][2].destination_config_id,
        ]
        await db.commit()
    seller = Seller({seed.panels[0].id: [F.vless("c0", "a.example", 443, remark="A"),
                                        F.vless("c1", "b.example", 443, remark="B"),
                                        F.vless("c2", "c.example", 443, remark="C")]})
    with _active_connection(), _patched(seller):
        got = link_identities(await _collect(seed))
    hosts = sorted(i[1] for i in got)
    assert hosts == ["a.example"], "only rows that are selected AND synced may be served; got " + str(hosts)


async def test_group_change_removes_host_immediately_without_waiting_for_cache():
    seed = await _seed([[F.vless("d0", "a.example", 443, remark="A"), F.vless("d1", "b.example", 443, remark="B")]])
    seller = Seller({seed.panels[0].id: [F.vless("c0", "a.example", 443, remark="A"),
                                        F.vless("c1", "b.example", 443, remark="B")]})
    with _active_connection(), _patched(seller):
        both = link_identities(await _collect(seed, seed.tags))
        only_first = link_identities(await _collect(seed, seed.tags[:1]))
    assert len(both) == 2
    assert [i[1] for i in only_first] == ["a.example"]


async def test_ambiguous_endpoint_resolves_by_remark_never_by_guessing_a_credential():
    """Two seller links on one host:port: the destination must get *its own* remark's credential."""
    seed = await _seed([[F.vless("d-a", "multi.example", 443, remark="Alpha"),
                         F.vless("d-b", "multi.example", 443, remark="Beta")]])
    seller = Seller({seed.panels[0].id: [F.vless("cust-alpha", "multi.example", 443, remark="Alpha"),
                                        F.vless("cust-beta", "multi.example", 443, remark="Beta")]})
    with _active_connection(), _patched(seller):
        links = await _collect(seed)
    by_remark = {}
    for link in links:
        from urllib.parse import unquote

        by_remark[unquote(link.rpartition("#")[2])] = link_identities([link])[0][3]
    assert by_remark.get("Alpha") == fp(F.cred("cust-alpha"))
    assert by_remark.get("Beta") == fp(F.cred("cust-beta"))


async def test_unmatched_destination_is_omitted_not_substituted_with_discovery_link():
    seed = await _seed([[F.vless("disc", "a.example", 443, remark="A"), F.vless("disc2", "b.example", 443, remark="B")]])
    seller = Seller({seed.panels[0].id: [F.vless("c0", "a.example", 443, remark="A")]})  # b missing
    with _active_connection(), _patched(seller):
        got = link_identities(await _collect(seed))
    assert [i[1] for i in got] == ["a.example"]
    assert fp(F.cred("disc2")) not in {i[3] for i in got}


# --------------------------------------------------------------------------------------------
# every format, same identity set, unsupported protocols mixed in
# --------------------------------------------------------------------------------------------


async def test_all_formats_serve_the_same_identities_for_a_mixed_two_panel_user():
    discovery = [
        [F.vless("d1", "ws.example", 443, remark="WS", network="ws"),
         F.vless("d2", "rl.example", 443, remark="RL", security="reality", flow="xtls-rprx-vision"),
         F.vmess("d3", "vm.example", 443, remark="VM")],
        [F.trojan("d4", "tj.example", 443, remark="TJ", network="grpc"),
         F.shadowsocks("d5", "ss.example", 8388, remark="SS"),
         F.hysteria2("d6", "hy.example", 443, remark="HY")],
    ]
    seed = await _seed(discovery)
    customer = [
        [F.vless("c1", "ws.example", 443, remark="WS", network="ws"),
         F.vless("c2", "rl.example", 443, remark="RL", security="reality", flow="xtls-rprx-vision"),
         F.vmess("c3", "vm.example", 443, remark="VM")],
        [F.trojan("c4", "tj.example", 443, remark="TJ", network="grpc"),
         F.shadowsocks("c5", "ss.example", 8388, remark="SS"),
         F.hysteria2("c6", "hy.example", 443, remark="HY")],
    ]
    seller = Seller({seed.panels[0].id: customer[0], seed.panels[1].id: customer[1]})
    with _active_connection(), _patched(seller):
        links_body = await _generate(seed, "links")
        outputs = {fmt: await _generate(seed, fmt) for fmt in ("xray", "sing_box", "clash_meta")}
    link_lines = [line for line in links_body.splitlines() if line.strip()]
    assert len(link_lines) == 6, "plain links keep every selected config including hysteria2"
    expected = sorted(link_identities(link_lines))
    assert len(expected) == 5  # hysteria2 has no structured identity
    for fmt, body in outputs.items():
        assert sorted(identities_for_format(body, fmt)) == expected, fmt
    # nothing from the discovery users may appear anywhere
    blob = links_body + "".join(outputs.values())
    for label in ("d1", "d2", "d3", "d4", "d5", "d6"):
        assert F.cred(label) not in blob


async def test_xray_output_is_never_an_empty_list_when_selected_hosts_exist():
    seed = await _seed([[F.vless("d", "a.example", 443, remark="A")]])
    seller = Seller({seed.panels[0].id: [F.vless("c", "a.example", 443, remark="A")]})
    with _active_connection(), _patched(seller):
        body = await _generate(seed, "xray")
    data = json.loads(body)
    assert isinstance(data, list) and len(data) == 1


async def test_classic_clash_yields_no_vless_but_does_not_fail():
    seed = await _seed([[F.vless("d", "a.example", 443, remark="A"), F.vmess("dm", "m.example", 443, remark="M")]])
    seller = Seller({seed.panels[0].id: [F.vless("c", "a.example", 443, remark="A"),
                                        F.vmess("cm", "m.example", 443, remark="M")]})
    with _active_connection(), _patched(seller):
        body = await _generate(seed, "clash")
    got = identities_for_format(body, "clash")
    assert [i[0] for i in got] == ["vmess"]
    assert yaml.safe_load(body)["proxies"]


# --------------------------------------------------------------------------------------------
# failure vs empty, fail-closed, caching of degraded results
# --------------------------------------------------------------------------------------------


async def test_all_sellers_failing_returns_503_with_retry_after_and_is_not_cached():
    seed = await _seed([[F.vless("d", "a.example", 443, remark="A")]])
    seller = Seller({seed.panels[0].id: TimeoutError("synthetic")})
    with _active_connection(), _patched(seller):
        with pytest.raises(HTTPException) as err:
            await _generate(seed, "xray")
        assert err.value.status_code == 503
        assert err.value.headers["Retry-After"] == "30"
        assert err.value.headers["Cache-Control"] == "no-store"
        # seller recovers: the very next request must succeed (nothing negative was cached)
        seller.behaviour[seed.panels[0].id] = [F.vless("c", "a.example", 443, remark="A")]
        body = await _generate(seed, "xray")
    assert len(json.loads(body)) == 1


async def test_fail_closed_can_be_disabled_by_environment(monkeypatch):
    monkeypatch.setenv("OC_SUB_UPSTREAM_FAIL_CLOSED", "0")
    seed = await _seed([[F.vless("d", "a.example", 443, remark="A")]])
    seller = Seller({seed.panels[0].id: TimeoutError("synthetic")})
    with _active_connection(), _patched(seller):
        body = await _generate(seed, "links")
    assert body.strip() == ""


async def test_partial_failure_serves_healthy_panel_and_does_not_cache_the_partial_body():
    seed = await _seed([[F.vless("d1", "a.example", 443, remark="A")], [F.vless("d2", "b.example", 443, remark="B")]])
    seller = Seller({
        seed.panels[0].id: [F.vless("c1", "a.example", 443, remark="A")],
        seed.panels[1].id: ConnectionError("synthetic"),
    })
    with _active_connection(), _patched(seller):
        partial = await _generate(seed, "links")
        assert [i[1] for i in link_identities(partial.splitlines())] == ["a.example"]
        seller.behaviour[seed.panels[1].id] = [F.vless("c2", "b.example", 443, remark="B")]
        full = await _generate(seed, "links")
    assert sorted(i[1] for i in link_identities(full.splitlines())) == ["a.example", "b.example"]


async def test_seller_returning_no_links_is_a_failure_not_a_successful_empty_pool():
    seed = await _seed([[F.vless("d", "a.example", 443, remark="A")]])
    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body", new=AsyncMock(return_value="")
    ):
        async with GetDB() as db:
            state = await load_oc_subscription_links_state(db, seed.uid, seed.tags, user_tenant_id=None)
            diag: dict = {}
            links = await build_oc_subscription_links_from_state(state, {}, diagnostics=diag)
    assert links == []
    assert diag["degraded"] and diag["all_failed"] and diag["failed_panels"] == [seed.panels[0].id]


async def test_stale_pool_is_used_when_seller_fails_within_bound(monkeypatch):
    monkeypatch.setattr(cache, "FRESH_TTL_S", 0.0)
    seed = await _seed([[F.vless("d", "a.example", 443, remark="A")]])
    seller = Seller({seed.panels[0].id: [F.vless("c", "a.example", 443, remark="A")]})
    with _active_connection(), _patched(seller):
        first = link_identities(await _collect(seed))
        seller.behaviour[seed.panels[0].id] = TimeoutError("synthetic")
        await asyncio.sleep(0.01)
        async with GetDB() as db:
            state = await load_oc_subscription_links_state(db, seed.uid, seed.tags, user_tenant_id=None)
            diag: dict = {}
            second = link_identities(await build_oc_subscription_links_from_state(state, {}, diagnostics=diag))
    assert first == second and first
    assert diag["stale_panels"] == [seed.panels[0].id] and not diag["failed_panels"]


async def test_stale_pool_is_not_used_after_the_staleness_bound(monkeypatch):
    monkeypatch.setattr(cache, "FRESH_TTL_S", 0.0)
    monkeypatch.setattr(cache, "STALE_MAX_AGE_S", 0.0)
    seed = await _seed([[F.vless("d", "a.example", 443, remark="A")]])
    seller = Seller({seed.panels[0].id: [F.vless("c", "a.example", 443, remark="A")]})
    with _active_connection(), _patched(seller):
        await _collect(seed)
        seller.behaviour[seed.panels[0].id] = TimeoutError("synthetic")
        await asyncio.sleep(0.01)
        async with GetDB() as db:
            state = await load_oc_subscription_links_state(db, seed.uid, seed.tags, user_tenant_id=None)
            diag: dict = {}
            links = await build_oc_subscription_links_from_state(state, {}, diagnostics=diag)
    assert links == [] and diag["failed_panels"] == [seed.panels[0].id]


async def test_stale_pool_never_crosses_a_mapping_url_change(monkeypatch):
    """Re-provisioned mapping URL = different identity: the old pool must not be served on error."""
    monkeypatch.setattr(cache, "FRESH_TTL_S", 0.0)
    seed = await _seed([[F.vless("d", "a.example", 443, remark="A")]])
    seller = Seller({seed.panels[0].id: [F.vless("old-owner", "a.example", 443, remark="A")]})
    with _active_connection(), _patched(seller):
        await _collect(seed)
        async with GetDB() as db:
            mapping = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == seed.uid))).scalar_one()
            mapping.external_subscription_url = "https://198.51.100.77/sub/reprovisioned"
            await db.commit()
        seller.behaviour[seed.panels[0].id] = TimeoutError("synthetic")
        async with GetDB() as db:
            state = await load_oc_subscription_links_state(db, seed.uid, seed.tags, user_tenant_id=None)
            diag: dict = {}
            links = await build_oc_subscription_links_from_state(state, {}, diagnostics=diag)
    assert links == [] and diag["failed_panels"] == [seed.panels[0].id]


# --------------------------------------------------------------------------------------------
# determinism and upstream call counts
# --------------------------------------------------------------------------------------------


async def test_output_order_is_independent_of_seller_response_timing():
    discovery = [[F.vless(f"d{p}{i}", f"h{p}{i}.example", 443, remark=f"R{p}{i}") for i in range(3)] for p in range(3)]
    seed = await _seed(discovery)

    def pool(p):
        return [F.vless(f"c{p}{i}", f"h{p}{i}.example", 443, remark=f"R{p}{i}") for i in range(3)]

    async def slow(p, delay):
        async def _inner():
            await asyncio.sleep(delay)
            return pool(p)

        return _inner

    bodies = []
    for delays in ((0.06, 0.03, 0.0), (0.0, 0.03, 0.06), (0.03, 0.0, 0.06)):
        cache.clear_upstream_links_cache()
        behaviour = {panel.id: await slow(p, delays[p]) for p, panel in enumerate(seed.panels)}
        from app.subscription.config_cache import clear_sub_config_cache

        clear_sub_config_cache()
        with _active_connection(), _patched(Seller(behaviour)):
            bodies.append(await _generate(seed, "xray"))
    assert bodies[0] == bodies[1] == bodies[2]


async def test_each_panel_is_fetched_once_per_request_and_cached_between_requests():
    seed = await _seed([[F.vless("d1", "a.example", 443, remark="A")], [F.vless("d2", "b.example", 443, remark="B")]])
    seller = Seller({
        seed.panels[0].id: [F.vless("c1", "a.example", 443, remark="A")],
        seed.panels[1].id: [F.vless("c2", "b.example", 443, remark="B")],
    })
    from app.subscription.config_cache import clear_sub_config_cache

    with _active_connection(), _patched(seller):
        await _generate(seed, "xray")
        assert sorted(seller.calls) == sorted(p.id for p in seed.panels), "exactly one fetch per panel"
        clear_sub_config_cache()  # bypass the 15s body cache: the 30s pool cache must still prevent refetch
        await _generate(seed, "sing_box")
    assert len(seller.calls) == 2


async def test_slow_seller_does_not_block_other_panels():
    seed = await _seed([[F.vless("d1", "a.example", 443, remark="A")], [F.vless("d2", "b.example", 443, remark="B")]])

    loop = asyncio.get_event_loop()
    spans: dict[str, tuple[float, float]] = {}

    def timed(name: str, remark: str, host: str):
        async def fetch():
            entered = loop.time()
            await asyncio.sleep(0.2)
            spans[name] = (entered, loop.time())
            return [F.vless(name, host, 443, remark=remark)]

        return fetch

    seller = Seller({seed.panels[0].id: timed("c1", "A", "a.example"), seed.panels[1].id: timed("c2", "B", "b.example")})
    with _active_connection(), _patched(seller):
        links = await _collect(seed)
    assert len(links) == 2
    # Deterministic (no wall-clock threshold): serial fetching could never overlap, concurrent fetching must.
    (a_in, a_out), (b_in, b_out) = spans["c1"], spans["c2"]
    assert a_in < b_out and b_in < a_out, "panels are fetched concurrently, not serially"


# --------------------------------------------------------------------------------------------
# upstream links cache (unit)
# --------------------------------------------------------------------------------------------


async def test_cache_single_flight_shares_one_fetch():
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return ["a"]

    key = cache.make_key(1, 1, "u", {"x"})
    results = await asyncio.gather(*(cache.get_user_links(key, fetcher) for _ in range(20)))
    assert calls == 1
    assert all(r.links == ["a"] for r in results)


async def test_cache_cancelled_leader_does_not_abort_shared_fetch():
    calls = 0

    async def fetcher():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return ["a"]

    key = cache.make_key(1, 1, "u", None)
    leader = asyncio.ensure_future(cache.get_user_links(key, fetcher))
    await asyncio.sleep(0.01)
    follower = asyncio.ensure_future(cache.get_user_links(key, fetcher))
    await asyncio.sleep(0.01)
    leader.cancel()
    result = await follower
    assert result.links == ["a"] and result.status == "fetched" and calls == 1


async def test_cache_failures_are_not_cached():
    attempts = 0

    async def fetcher():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("synthetic")
        return ["ok"]

    key = cache.make_key(1, 1, "u", None)
    first = await cache.get_user_links(key, fetcher)
    second = await cache.get_user_links(key, fetcher)
    assert (first.status, first.links) == ("failed", [])
    assert (second.status, second.links) == ("fetched", ["ok"])


async def test_cache_keys_isolate_user_panel_url_and_sync_set():
    base = cache.make_key(1, 1, "u", {"a"})
    assert base == cache.make_key(1, 1, "u", {"a"})
    for other in (
        cache.make_key(2, 1, "u", {"a"}),
        cache.make_key(1, 2, "u", {"a"}),
        cache.make_key(1, 1, "u2", {"a"}),
        cache.make_key(1, 1, "u", {"a", "b"}),
    ):
        assert other != base
    assert "u" not in repr(base), "URL must be stored only as a non-reversible fingerprint"


async def test_cache_is_bounded():
    for i in range(cache.MAX_ENTRIES + 50):
        cache._remember(cache.make_key(i, 1, "u", None), ["x"])
    assert len(cache._entries) <= cache.MAX_ENTRIES
