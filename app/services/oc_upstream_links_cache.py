"""Bounded cache for a customer's own upstream (seller panel) share-link list.

Why this exists
---------------
Every OC subscription request used to hit the seller panel for the user's link list, with the
result of a failed fetch silently treated as "no links".  That made the response depend on seller
latency/availability: a slow or failing seller produced empty/partial bodies, and the final body
was then cached for several seconds, so the *next* manual refresh could look different.

Design (deliberately conservative)
----------------------------------
* The cache holds only the customer's raw upstream link pool.  Group/tag authorization is applied
  afterwards from the database on every request, so serving a cached pool can never widen or
  narrow the user's group selection.
* The key covers user, panel, the mapping URL fingerprint and the synced-config fingerprint, so a
  group change (which rewrites ``last_synced_configs``), a re-provisioned URL, or a different panel
  can never reuse another pool.
* Fresh entries (``FRESH_TTL_S``) are served without network.  Expired entries are only served
  when the live fetch *fails* (stale-if-error, bounded by ``STALE_MAX_AGE_S``) and the caller is
  told it was stale.  Successful empty pools are real results and are cached like any other pool.
* Failures are never cached.  Concurrent requests for the same key share a single in-flight fetch
  (single-flight) so a client retrying after its own timeout joins the running fetch instead of
  starting a second one against the seller.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

FRESH_TTL_S = 30.0
STALE_MAX_AGE_S = 6 * 3600.0
MAX_ENTRIES = 2048

FetchStatus = str  # "fresh" | "fetched" | "stale" | "failed"


class OcUpstreamUnavailableError(Exception):
    """Required seller pools could not be loaded and nothing else can be served for the request."""


@dataclass(frozen=True)
class UpstreamLinksResult:
    links: list[str]
    status: FetchStatus
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status != "failed"


_entries: OrderedDict[tuple, tuple[float, list[str]]] = OrderedDict()
_inflight: dict[tuple, asyncio.Future] = {}  # key -> running fetch task


def synced_fingerprint(synced: set[str] | frozenset[str] | None) -> str:
    joined = "\x1f".join(sorted(synced or ()))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def url_fingerprint(url: str | None) -> str:
    return hashlib.sha256((url or "").strip().encode()).hexdigest()[:16]


def make_key(user_id: int, panel_id: int, url: str | None, synced: set[str] | frozenset[str] | None) -> tuple:
    return (int(user_id), int(panel_id), url_fingerprint(url), synced_fingerprint(synced))


def clear_upstream_links_cache() -> None:
    _entries.clear()
    _inflight.clear()


def _remember(key: tuple, links: list[str]) -> None:
    _entries[key] = (time.monotonic(), list(links))
    _entries.move_to_end(key)
    while len(_entries) > MAX_ENTRIES:
        _entries.popitem(last=False)


def _lookup(key: tuple) -> tuple[float, list[str]] | None:
    item = _entries.get(key)
    if item is None:
        return None
    stored_at, links = item
    return time.monotonic() - stored_at, list(links)  # type: ignore[return-value]


async def get_user_links(
    key: tuple,
    fetcher: Callable[[], Awaitable[list[str]]],
    *,
    fresh_ttl_s: float | None = None,
    stale_max_age_s: float | None = None,
) -> UpstreamLinksResult:
    """
    Return the upstream link pool for ``key``.

    ``fetcher`` must return the *complete* list of share links (possibly empty) or raise.  Any
    exception is treated as a failed fetch: the stale pool is used when still within bounds,
    otherwise the result is ``failed`` (never an empty success).
    """
    fresh_ttl = FRESH_TTL_S if fresh_ttl_s is None else fresh_ttl_s
    stale_max = STALE_MAX_AGE_S if stale_max_age_s is None else stale_max_age_s

    hit = _lookup(key)
    if hit is not None and hit[0] <= fresh_ttl:
        _entries.move_to_end(key)
        return UpstreamLinksResult(links=hit[1], status="fresh")

    task = _inflight.get(key)
    if task is None:

        async def _run() -> UpstreamLinksResult:
            try:
                links = await fetcher()
                result = UpstreamLinksResult(links=list(links), status="fetched")
                _remember(key, result.links)
                return result
            except Exception as exc:  # noqa: BLE001 - classified, never swallowed silently
                error = type(exc).__name__
                stale = _lookup(key)
                if stale is not None and stale[0] <= stale_max:
                    return UpstreamLinksResult(links=stale[1], status="stale", error=error)
                return UpstreamLinksResult(links=[], status="failed", error=error)
            finally:
                _inflight.pop(key, None)

        # Independent task: a cancelled/disconnected caller must not abort the shared fetch, so a
        # client retry joins it (or hits the cache it populates) instead of starting another.
        task = asyncio.ensure_future(_run())
        _inflight[key] = task
    return await asyncio.shield(task)
