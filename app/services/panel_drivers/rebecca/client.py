"""Async Rebecca panel HTTP client (buyer sub-admin credentials)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.services.panel_drivers.exceptions import (
    ClientNotFoundError,
    InsufficientPermissionsError,
    PanelAuthError,
    PanelConnectionError,
    PanelResponseError,
)
from app.services.panel_drivers.http_utils import (
    connection_error_message,
    extract_error_detail,
    is_upstream_unavailable,
    looks_like_html_response,
    looks_like_invalid_credentials,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=15.0, pool=5.0)


def _normalize_base_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise PanelConnectionError("Panel URL is required")
    parsed = urlparse(raw)
    if not parsed.scheme:
        raw = f"https://{raw}"
    return raw.rstrip("/")


def _api_url(base: str, path: str) -> str:
    return urljoin(f"{_normalize_base_url(base)}/", path.lstrip("/"))


def _parse_list_payload(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


@dataclass
class NewUserPayload:
    username: str
    data_limit: int = 0
    expire: int = 0
    status: str = "active"
    on_hold_expire_duration: int | None = None
    hwid_limit: int | None = None
    service_id: int | None = None
    service_ids: list[int] | None = None
    proxies: dict[str, Any] = field(default_factory=dict)
    inbounds: dict[str, list[str]] = field(default_factory=dict)

    def to_api_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "username": self.username,
            "data_limit": self.data_limit,
            "status": self.status,
        }
        if self.status == "on_hold" and self.on_hold_expire_duration:
            body["on_hold_expire_duration"] = self.on_hold_expire_duration
        elif self.expire:
            body["expire"] = self.expire
        if self.service_ids:
            body["services"] = self.service_ids
        elif self.service_id is not None:
            body["service_id"] = self.service_id
        if self.proxies:
            body["proxies"] = self.proxies
        if self.inbounds:
            body["inbounds"] = self.inbounds
        if self.hwid_limit is not None:
            body["hwid_limit"] = self.hwid_limit
        return body


class RebeccaPanel:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._username = username
        self._password = password
        self._http: httpx.AsyncClient | None = None
        self._access_token: str | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, follow_redirects=True, verify=True)
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()
        self._http = None
        self._access_token = None

    async def login(self, *, force: bool = False) -> str:
        if force:
            self._access_token = None
        client = await self._client()
        url = _api_url(self._base_url, "/api/admin/token")
        try:
            response = await client.post(
                url,
                data={"username": self._username, "password": self._password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as exc:
            raise PanelConnectionError(f"Rebecca panel timed out during login: {self._base_url}") from exc
        except httpx.RequestError as exc:
            raise PanelConnectionError(f"Cannot reach Rebecca panel at {self._base_url}: {exc}") from exc

        raw_text = response.text[:500] if response.text else ""
        content_type = response.headers.get("content-type")
        body: Any = None
        if response.content and not looks_like_html_response(raw_text, content_type):
            try:
                body = response.json()
            except ValueError:
                body = None

        if is_upstream_unavailable(response.status_code, raw_text, content_type):
            raise PanelConnectionError(
                connection_error_message(method="POST", url=url, status_code=response.status_code, raw_text=raw_text)
            )
        if looks_like_invalid_credentials(response.status_code, body, raw_text):
            raise PanelAuthError("Invalid Rebecca panel credentials")
        if response.status_code >= 400:
            detail = extract_error_detail(body, raw_text)
            raise PanelResponseError(
                f"Rebecca login failed with HTTP {response.status_code}" + (f": {detail}" if detail else ""),
                raw=body if isinstance(body, dict) else None,
            )

        if not isinstance(body, dict) or not body.get("access_token"):
            raise PanelAuthError("Rebecca panel did not return an access token")
        self._access_token = str(body["access_token"])
        return self._access_token

    async def _ensure_auth(self) -> None:
        if not self._access_token:
            await self.login()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        operation: str = "panel API call",
        expected_status: tuple[int, ...] = (200, 201, 204),
        retry_auth: bool = True,
    ) -> Any:
        await self._ensure_auth()
        client = await self._client()
        url = _api_url(self._base_url, path)
        try:
            response = await client.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                json=json_body,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise PanelConnectionError(f"Rebecca panel timed out [{method} {path}]: {exc}") from exc
        except httpx.RequestError as exc:
            raise PanelConnectionError(f"Rebecca panel unreachable [{method} {path}]: {exc}") from exc

        raw_text = response.text[:500] if response.text else ""
        content_type = response.headers.get("content-type")
        body: Any = None
        if response.content and not looks_like_html_response(raw_text, content_type):
            try:
                body = response.json()
            except ValueError:
                body = None

        if response.status_code == 401 and retry_auth:
            await self.login(force=True)
            return await self._request(
                method,
                path,
                json_body=json_body,
                params=params,
                operation=operation,
                expected_status=expected_status,
                retry_auth=False,
            )

        if is_upstream_unavailable(response.status_code, raw_text, content_type):
            raise PanelConnectionError(
                connection_error_message(method=method, url=url, status_code=response.status_code, raw_text=raw_text)
            )

        if response.status_code in expected_status:
            if not response.content:
                return {}
            return body if body is not None else raw_text

        if response.status_code == 404:
            raise ClientNotFoundError(extract_error_detail(body, raw_text) or f"Rebecca resource not found: {path}")

        if response.status_code == 403:
            raise InsufficientPermissionsError(
                extract_error_detail(body, raw_text) or f"Insufficient permissions for {operation}",
                raw=body if isinstance(body, dict) else None,
            )

        detail = extract_error_detail(body, raw_text)
        raise PanelResponseError(
            f"Rebecca {operation} failed with HTTP {response.status_code}" + (f": {detail}" if detail else ""),
            raw=body if isinstance(body, dict) else None,
        )

    async def list_services(self, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/api/v2/services",
            params={"offset": offset, "limit": limit},
            operation="list services",
        )
        if isinstance(payload, list):
            return {"services": payload, "total": len(payload)}
        if isinstance(payload, dict):
            items = _parse_list_payload(payload, "services", "items", "data")
            total = int(payload.get("total") or len(items))
            return {"services": items, "total": total}
        return {"services": [], "total": 0}

    async def list_all_service_ids(self) -> list[int]:
        ids: list[int] = []
        offset = 0
        limit = 100
        while True:
            page = await self.list_services(offset=offset, limit=limit)
            services = page.get("services") or []
            if not isinstance(services, list):
                break
            for item in services:
                if not isinstance(item, dict):
                    continue
                sid = item.get("id") if item.get("id") is not None else item.get("service_id")
                if sid is not None:
                    ids.append(int(sid))
            offset += len(services)
            total = int(page.get("total") or 0)
            if not services or offset >= total:
                break
        return ids

    async def list_inbounds(self) -> dict[str, Any]:
        payload = await self._request("GET", "/api/inbounds", operation="list inbounds")
        return payload if isinstance(payload, dict) else {}

    async def fetch_all_inbound_assignment(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        grouped = await self.list_inbounds()
        for protocol, entries in grouped.items():
            if not isinstance(entries, list):
                continue
            tags: list[str] = []
            for entry in entries:
                if isinstance(entry, dict) and entry.get("tag"):
                    tags.append(str(entry["tag"]))
                elif entry:
                    tags.append(str(entry))
            if tags:
                result[str(protocol)] = tags
        return result

    async def get_user(self, username: str) -> dict:
        payload = await self._request("GET", f"/api/user/{username}", operation="get user")
        if not isinstance(payload, dict):
            raise PanelResponseError(f"Unexpected Rebecca user response for {username!r}")
        return payload

    async def create_user(self, payload: NewUserPayload) -> dict:
        await self._request(
            "POST",
            "/api/user",
            json_body=payload.to_api_dict(),
            operation="create user",
            expected_status=(200, 201),
        )
        return await self.get_user(payload.username)

    async def update_user(self, username: str, **fields: Any) -> dict:
        updated = await self._request(
            "PUT",
            f"/api/user/{username}",
            json_body=fields,
            operation="update user",
        )
        return updated if isinstance(updated, dict) else await self.get_user(username)

    async def delete_user(self, username: str) -> None:
        await self._request(
            "DELETE",
            f"/api/user/{username}",
            operation="delete user",
            expected_status=(200, 204),
        )
