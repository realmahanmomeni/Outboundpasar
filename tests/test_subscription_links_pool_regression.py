"""DB pool regression: subscription link upstream fetch and OC usage accounting concurrency."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import SubscriptionCustomDomainState
from app.db.models_oc import OCUserMapping
from app.middlewares import custom_domain_host as cdh
from app.node.oc_usage import ACCOUNT_MAPPING_USAGE_SEM
from app.services.oc_user_subscription_hosts import (
    PANEL_UPSTREAM_FETCH_CONCURRENCY,
    _fetch_upstream_links_by_panel,
)


@pytest.mark.asyncio
async def test_upstream_fetch_runs_after_db_session_scope():
    db_open = {"open": False}

    async def _probe_fetch(*_args, **_kwargs):
        assert db_open["open"] is False, "upstream fetch must not run while GetDB is held"
        return "vless://11111111-1111-1111-1111-111111111111@1.1.1.1:443?encryption=none#x"

    mapping = OCUserMapping(
        user_id=1,
        panel_id=10,
        external_user_id="e",
        external_subscription_url="https://panel.example/sub",
    )
    mappings = {10: mapping}

    with patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(side_effect=_probe_fetch),
    ):
        db_open["open"] = True
        db_open["open"] = False
        await _fetch_upstream_links_by_panel(mappings, {10})


@pytest.mark.asyncio
async def test_panel_upstream_fetch_concurrency_bounded():
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def _slow_fetch(_mapping: OCUserMapping):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.03)
        async with lock:
            active -= 1
        return ["vless://11111111-1111-1111-1111-111111111111@1.1.1.1:443?encryption=none#x"]

    mappings = {
        i: OCUserMapping(
            user_id=1,
            panel_id=i,
            external_user_id=f"e{i}",
            external_subscription_url=f"https://panel{i}.example/sub",
        )
        for i in range(1, 7)
    }
    with patch("app.services.oc_user_subscription_hosts._fetch_user_links", side_effect=_slow_fetch):
        await _fetch_upstream_links_by_panel(mappings, set(range(1, 7)))
    assert peak <= PANEL_UPSTREAM_FETCH_CONCURRENCY


@pytest.mark.asyncio
async def test_panel_fetch_failure_does_not_block_other_panels():
    async def _fetch(mapping: OCUserMapping):
        if mapping.panel_id == 2:
            return None
        return ["vless://11111111-1111-1111-1111-111111111111@1.1.1.1:443?encryption=none#x"]

    mappings = {
        1: OCUserMapping(user_id=1, panel_id=1, external_user_id="a", external_subscription_url="https://a/sub"),
        2: OCUserMapping(user_id=1, panel_id=2, external_user_id="b", external_subscription_url="https://b/sub"),
        3: OCUserMapping(user_id=1, panel_id=3, external_user_id="c", external_subscription_url="https://c/sub"),
    }
    with patch("app.services.oc_user_subscription_hosts._fetch_user_links", side_effect=_fetch):
        links = await _fetch_upstream_links_by_panel(mappings, {1, 2, 3})
    assert links[1]
    assert links[2] == []
    assert links[3]


@pytest.mark.asyncio
async def test_account_mapping_usage_semaphore_limits_parallel_db_writes():
    peak = 0
    active = 0
    lock = asyncio.Lock()

    async def _work() -> None:
        nonlocal peak, active
        async with ACCOUNT_MAPPING_USAGE_SEM:
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.02)
            async with lock:
                active -= 1

    await asyncio.gather(*(_work() for _ in range(9)))
    assert peak <= 3


def test_custom_domain_hostname_cache_hit_miss_and_expiry(monkeypatch):
    cdh.clear_custom_domain_hostname_cache()
    hostname = "sub.example.com"
    domain = cdh.SubscriptionCustomDomain(
        workspace_id=1,
        tenant_id=1,
        hostname_normalized="sub.example.com",
        domain_kind="arbitrary",
        state=SubscriptionCustomDomainState.active,
    )
    times = iter([100.0, 100.5, 109.0, 109.0, 109.5])
    monkeypatch.setattr(cdh, "monotonic", lambda: next(times, 200.0))

    cdh._cache_set(hostname, domain)
    hit = cdh._cache_get(hostname)
    assert hit is not None
    assert hit.workspace_id == 1

    miss = cdh._cache_get(hostname)
    assert miss is cdh._CACHE_MISS

    cdh._cache_set(hostname, None)
    assert cdh._cache_get(hostname) is None
