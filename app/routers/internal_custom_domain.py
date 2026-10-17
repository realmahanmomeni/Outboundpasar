"""Internal endpoints for edge TLS integration (not part of the public admin API)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response

from app.db import AsyncSession, get_db
from app.services.custom_domain.proxy_permission import hostname_may_receive_tls_certificate
from config import custom_domain_settings, server_settings

router = APIRouter(include_in_schema=False)


def _client_is_trusted(request: Request) -> bool:
    if not request.client:
        return False
    host = request.client.host
    allowed = server_settings.forwarded_allow_ips
    if isinstance(allowed, str):
        allowed_hosts = {item.strip() for item in allowed.split(",") if item.strip()}
    else:
        allowed_hosts = set(allowed)
    return host in allowed_hosts


@router.get("/internal/caddy-tls-permission")
async def caddy_tls_permission(
    request: Request,
    domain: str = Query(min_length=3, max_length=253),
    secret: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """
    Caddy ``on_demand_tls { ask ... }`` permission hook.

    Returns 200 only for registered, verified hostnames that are eligible for managed TLS.
    """
    if custom_domain_settings.caddy_ask_secret:
        if secret != custom_domain_settings.caddy_ask_secret:
            return Response(status_code=status.HTTP_403_FORBIDDEN)
    elif not _client_is_trusted(request):
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    if await hostname_may_receive_tls_certificate(db, domain):
        return Response(status_code=status.HTTP_200_OK)
    return Response(status_code=status.HTTP_403_FORBIDDEN)
