"""Restrict custom subscription hostnames to subscription routes when active."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime as dt
from time import monotonic

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.db import GetDB
from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState
from app.services.custom_domain.host_resolution import get_domain_by_hostname
from app.services.custom_domain.normalization import normalize_custom_domain_hostname
from app.services.custom_domain.ssl_lifecycle import domain_is_routable_for_subscriptions
from config import custom_domain_settings, subscription_env_settings

_HOSTNAME_LOOKUP_TTL_SECONDS = 8.0
_hostname_lookup_cache: dict[str, tuple[float, _CachedCustomDomain | None]] = {}
_CACHE_MISS = object()


@dataclass(frozen=True)
class _CachedCustomDomain:
    workspace_id: int
    tenant_id: int
    hostname_normalized: str
    domain_kind: str
    state: SubscriptionCustomDomainState
    certificate_not_after: dt | None
    oc_last_validated_at: dt | None


def _snapshot_domain(domain: SubscriptionCustomDomain) -> _CachedCustomDomain:
    return _CachedCustomDomain(
        workspace_id=domain.workspace_id,
        tenant_id=domain.tenant_id,
        hostname_normalized=domain.hostname_normalized,
        domain_kind=domain.domain_kind,
        state=domain.state,
        certificate_not_after=domain.certificate_not_after,
        oc_last_validated_at=domain.oc_last_validated_at,
    )


def _domain_from_snapshot(snap: _CachedCustomDomain) -> SubscriptionCustomDomain:
    return SubscriptionCustomDomain(
        workspace_id=snap.workspace_id,
        tenant_id=snap.tenant_id,
        hostname_normalized=snap.hostname_normalized,
        domain_kind=snap.domain_kind,
        state=snap.state,
        certificate_not_after=snap.certificate_not_after,
        oc_last_validated_at=snap.oc_last_validated_at,
    )


def _cache_get(hostname: str):
    try:
        normalized = normalize_custom_domain_hostname(hostname)
    except Exception:
        return _CACHE_MISS
    entry = _hostname_lookup_cache.get(normalized)
    if entry is None:
        return _CACHE_MISS
    expires_at, snap = entry
    if monotonic() >= expires_at:
        _hostname_lookup_cache.pop(normalized, None)
        return _CACHE_MISS
    if snap is None:
        return None
    return _domain_from_snapshot(snap)


def _cache_set(hostname: str, domain: SubscriptionCustomDomain | None) -> None:
    try:
        normalized = normalize_custom_domain_hostname(hostname)
    except Exception:
        return
    snap = _snapshot_domain(domain) if domain is not None else None
    _hostname_lookup_cache[normalized] = (monotonic() + _HOSTNAME_LOOKUP_TTL_SECONDS, snap)


def clear_custom_domain_hostname_cache() -> None:
    """Test helper: reset in-memory hostname resolution cache."""
    _hostname_lookup_cache.clear()


def _subscription_path_prefix() -> str:
    return f"/{subscription_env_settings.path}"


def _is_subscription_path(path: str) -> bool:
    prefix = _subscription_path_prefix()
    return path == prefix or path.startswith(f"{prefix}/")


class CustomDomainHostMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        hostname = request.url.hostname
        if not hostname:
            return await call_next(request)

        cached = _cache_get(hostname)
        if cached is _CACHE_MISS:
            async with GetDB() as db:
                domain = await get_domain_by_hostname(db, hostname)
            _cache_set(hostname, domain)
        else:
            domain = cached

        if domain is None:
            return await call_next(request)

        routable = await domain_is_routable_for_subscriptions(
            domain,
            public_https_ready=custom_domain_settings.ingress_public_https_ready,
        )
        if routable:
            request.state.subscription_custom_domain_workspace_id = domain.workspace_id
            if not _is_subscription_path(request.url.path):
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            return await call_next(request)

        if domain.state == SubscriptionCustomDomainState.disabled:
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        if _is_subscription_path(request.url.path):
            return JSONResponse(
                status_code=503,
                content={"detail": "Custom domain is not active for subscription traffic"},
            )

        return JSONResponse(status_code=404, content={"detail": "Not found"})
