"""Caddy admin API integration for custom subscription hostnames (HTTP-01 via edge proxy)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime as dt, timedelta as td
from typing import Any
from urllib.parse import quote

import httpx

from app.db.models import SubscriptionCustomDomain
from app.services.custom_domain.ingress_provider import IngressProviderKind, IngressProviderStatus
from app.services.custom_domain.ssl_lifecycle import (
    CertificateProvisioner,
    CertificateProvisionerError,
    CertificateProviderUnavailable,
    CertificateRateLimited,
)

_CADDY_SERVER_ID = "pasarguard_custom_domains"
_ROUTE_ID_PREFIX = "custom-domain-"


def _safe_admin_message(message: str) -> str:
    return message[:256] if message else "Caddy admin API error"


class CaddyCertificateProvisioner(CertificateProvisioner):
    """
    Register explicit hostname routes on a dedicated Caddy HTTP server and rely on Caddy's
    managed ACME (typically HTTP-01 on :443/:80) for certificate lifecycle.

    Requires CUSTOM_DOMAIN_CADDY_ADMIN_URL (e.g. http://127.0.0.1:2019) and a Caddy config
    that does not enable permissive on-demand TLS for arbitrary hosts.
    """

    def __init__(
        self,
        admin_base_url: str,
        *,
        upstream_dial: str = "127.0.0.1:8000",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._admin_base_url = admin_base_url.rstrip("/")
        self._upstream_dial = upstream_dial
        self._client = http_client
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def provider_kind(self) -> IngressProviderKind:
        return IngressProviderKind.caddy

    async def provider_status(self, *, public_https_ready: bool) -> IngressProviderStatus:
        try:
            resp = await self._request("GET", "/config/")
        except CertificateProviderUnavailable as exc:
            return IngressProviderStatus(
                provider=IngressProviderKind.caddy,
                configured=True,
                reachable=False,
                routing_capable=False,
                public_https_ready=public_https_ready,
                message=_safe_admin_message(str(exc)),
            )
        reachable = resp.status_code == 200
        routing_capable = reachable and public_https_ready
        message = "Caddy admin API reachable" if reachable else f"Caddy admin API unhealthy ({resp.status_code})"
        if reachable and not public_https_ready:
            message += "; public HTTPS ingress not marked ready (CUSTOM_DOMAIN_INGRESS_PUBLIC_HTTPS_READY)"
        return IngressProviderStatus(
            provider=IngressProviderKind.caddy,
            configured=True,
            reachable=reachable,
            routing_capable=routing_capable,
            public_https_ready=public_https_ready,
            message=message,
        )

    def _lock_for(self, domain_id: int) -> asyncio.Lock:
        return self._locks.setdefault(domain_id, asyncio.Lock())

    async def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(base_url=self._admin_base_url, timeout=httpx.Timeout(15.0, connect=5.0))

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        client = await self._http()
        own_client = self._client is None
        try:
            response = await client.request(method, path, **kwargs)
        finally:
            if own_client:
                await client.aclose()
        if response.status_code == 429:
            raise CertificateRateLimited("Caddy admin API rate limited")
        if response.status_code >= 500:
            raise CertificateProviderUnavailable(f"Caddy admin API error ({response.status_code})")
        return response

    async def ensure_custom_domain_server(self) -> None:
        """Ensure the dedicated custom-domain server block exists (idempotent)."""
        get_resp = await self._request("GET", f"/config/apps/http/servers/{_CADDY_SERVER_ID}")
        if get_resp.status_code == 200:
            return
        if get_resp.status_code not in {404, 400}:
            raise CertificateProviderUnavailable(
                f"unexpected Caddy admin response when reading server ({get_resp.status_code})"
            )
        server_config: dict[str, Any] = {
            "listen": [":443"],
            "routes": [],
            "tls_connection_policies": [{}],
        }
        put_resp = await self._request(
            "PUT",
            f"/config/apps/http/servers/{_CADDY_SERVER_ID}",
            json=server_config,
        )
        if put_resp.status_code not in {200, 201}:
            raise CertificateProviderUnavailable(
                f"failed to create Caddy custom-domain server ({put_resp.status_code})"
            )

    async def _find_route_index(self, hostname: str) -> int | None:
        resp = await self._request("GET", f"/config/apps/http/servers/{_CADDY_SERVER_ID}/routes")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CertificateProviderUnavailable(f"failed to list Caddy routes ({resp.status_code})")
        routes = resp.json()
        if not isinstance(routes, list):
            raise CertificateProviderUnavailable("malformed Caddy routes payload")
        for index, route in enumerate(routes):
            if not isinstance(route, dict):
                continue
            match = route.get("match")
            if not match:
                continue
            hosts = match[0].get("host") if isinstance(match, list) and match else None
            if isinstance(hosts, list) and hostname in hosts:
                return index
        return None

    def _route_body(self, domain: SubscriptionCustomDomain) -> dict[str, Any]:
        hostname = domain.hostname_normalized
        return {
            "@id": f"{_ROUTE_ID_PREFIX}{domain.id}",
            "match": [{"host": [hostname]}],
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "upstreams": [{"dial": self._upstream_dial}],
                    "headers": {"request": {"set": {"X-Forwarded-Host": [hostname]}}},
                }
            ],
            "terminal": True,
        }

    async def upsert_route(self, domain: SubscriptionCustomDomain) -> None:
        await self.ensure_custom_domain_server()
        body = self._route_body(domain)
        existing = await self._find_route_index(domain.hostname_normalized)
        if existing is None:
            resp = await self._request(
                "POST",
                f"/config/apps/http/servers/{_CADDY_SERVER_ID}/routes",
                json=body,
            )
        else:
            resp = await self._request(
                "PATCH",
                f"/config/apps/http/servers/{_CADDY_SERVER_ID}/routes/{existing}",
                json=body,
            )
        if resp.status_code not in {200, 201}:
            raise CertificateProviderUnavailable(f"failed to upsert Caddy route ({resp.status_code})")

    async def remove_route(self, domain: SubscriptionCustomDomain) -> None:
        index = await self._find_route_index(domain.hostname_normalized)
        if index is None:
            return
        resp = await self._request(
            "DELETE",
            f"/config/apps/http/servers/{_CADDY_SERVER_ID}/routes/{index}",
        )
        if resp.status_code not in {200, 204, 404}:
            raise CertificateProviderUnavailable(f"failed to remove Caddy route ({resp.status_code})")

    async def hostname_routing_ready(self, domain: SubscriptionCustomDomain) -> bool:
        return await self._find_route_index(domain.hostname_normalized) is not None

    async def certificate_ready(self, domain: SubscriptionCustomDomain) -> bool:
        try:
            not_after = await self._certificate_not_after(domain.hostname_normalized, allow_pending=False)
        except CertificateProvisionerError:
            return False
        return not_after > dt.now(UTC)

    async def _certificate_not_after(self, hostname: str, *, allow_pending: bool = False) -> dt:
        encoded = quote(hostname, safe="")
        resp = await self._request("GET", f"/pki/certificates/local/{encoded}")
        if resp.status_code == 404:
            raise CertificateProvisionerError("certificate is not yet issued")
        if resp.status_code != 200:
            raise CertificateProviderUnavailable(f"failed to read Caddy certificate ({resp.status_code})")
        payload = resp.json()
        if not isinstance(payload, dict):
            raise CertificateProviderUnavailable("malformed Caddy certificate payload")
        expires = payload.get("expires") or payload.get("not_after")
        if isinstance(expires, str):
            parsed = dt.fromisoformat(expires.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        if isinstance(expires, (int, float)):
            return dt.fromtimestamp(expires, tz=UTC)
        raise CertificateProviderUnavailable("Caddy certificate expiry unavailable")

    async def issue(self, domain: SubscriptionCustomDomain) -> tuple[str, dt]:
        async with self._lock_for(domain.id):
            await self.upsert_route(domain)
            try:
                not_after = await self._certificate_not_after(domain.hostname_normalized, allow_pending=True)
            except CertificateProvisionerError:
                raise CertificateProviderUnavailable("certificate is not yet issued by Caddy")
            if not_after <= dt.now(UTC):
                raise CertificateProvisionerError("certificate is not yet valid")
            ref = f"caddy:{domain.hostname_normalized}"
            return ref, not_after

    async def deprovision(self, domain: SubscriptionCustomDomain) -> None:
        async with self._lock_for(domain.id):
            await self.remove_route(domain)
