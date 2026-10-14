"""Restrict custom subscription hostnames to subscription routes when active."""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.db import GetDB
from app.db.models import SubscriptionCustomDomainState
from app.services.custom_domain.host_resolution import get_domain_by_hostname
from app.services.custom_domain.ssl_lifecycle import domain_is_routable_for_subscriptions
from config import custom_domain_settings, subscription_env_settings


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

        async with GetDB() as db:
            domain = await get_domain_by_hostname(db, hostname)

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
