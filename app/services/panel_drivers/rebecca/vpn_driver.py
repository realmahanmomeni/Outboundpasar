"""Rebecca buyer sub-admin VPN operations for PasarGuard integration."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from app.services.panel_drivers.exceptions import (
    ClientExistsError,
    ClientNotFoundError,
    PanelAdapterError,
    PanelResponseError,
)
from app.services.panel_drivers.rebecca.client import NewUserPayload, RebeccaPanel
from app.services.panel_drivers.types import VpnGroupInfo, VpnInboundInfo, VpnUserInfo


def _subscription_url(record: dict[str, Any], base_url: str) -> str | None:
    for key in ("subscription_url", "subscription_link", "link"):
        raw = str(record.get(key) or "").strip()
        if not raw:
            continue
        if raw.lower().startswith(("http://", "https://")):
            return raw
        return urljoin(f"{base_url.rstrip('/')}/", raw.lstrip("/"))
    return None


def _user_group_ids(record: dict[str, Any]) -> tuple[str, ...]:
    services = record.get("services") or record.get("service_ids") or []
    if isinstance(services, list):
        out: list[str] = []
        for item in services:
            if isinstance(item, dict) and item.get("id") is not None:
                out.append(str(item["id"]))
            elif item is not None:
                out.append(str(item))
        return tuple(out)
    if record.get("service_id") is not None:
        return (str(record["service_id"]),)
    return ()


def _to_vpn_user_info(record: dict[str, Any], base_url: str) -> VpnUserInfo:
    used = int(record.get("used_traffic") or record.get("used_bytes") or 0)
    limit_raw = record.get("data_limit")
    data_limit = int(limit_raw) if limit_raw is not None else None
    return VpnUserInfo(
        username=str(record.get("username") or ""),
        status=str(record.get("status") or ""),
        group_ids=_user_group_ids(record),
        subscription_url=_subscription_url(record, base_url),
        used_bytes=used,
        data_limit=data_limit,
    )


class RebeccaVpnDriver:
    async def list_vpn_groups(self, url: str, username: str, password: str) -> list[VpnGroupInfo]:
        panel = RebeccaPanel(url, username, password)
        try:
            await panel.login()
            service_ids = await panel.list_all_service_ids()
            inbounds_map = await panel.fetch_all_inbound_assignment()
            all_tags = sorted({t for tags in inbounds_map.values() for t in tags})
            if service_ids:
                groups: list[VpnGroupInfo] = []
                offset = 0
                names: dict[int, str] = {}
                while True:
                    page = await panel.list_services(offset=offset, limit=100)
                    for item in page.get("services") or []:
                        if not isinstance(item, dict):
                            continue
                        sid = item.get("id") if item.get("id") is not None else item.get("service_id")
                        if sid is None:
                            continue
                        names[int(sid)] = str(item.get("name") or item.get("title") or sid)
                    services = page.get("services") or []
                    if not services or offset + len(services) >= int(page.get("total") or 0):
                        break
                    offset += len(services)
                for sid in service_ids:
                    groups.append(
                        VpnGroupInfo(
                            id=str(sid),
                            name=names.get(sid, str(sid)),
                            inbound_tags=tuple(all_tags),
                        )
                    )
                return groups
            if inbounds_map:
                return [
                    VpnGroupInfo(id="default", name="default", inbound_tags=tuple(all_tags)),
                ]
            return []
        finally:
            await panel.close()

    async def list_vpn_inbounds(self, url: str, username: str, password: str) -> list[VpnInboundInfo]:
        panel = RebeccaPanel(url, username, password)
        try:
            await panel.login()
            inbounds: list[VpnInboundInfo] = []
            grouped = await panel.list_inbounds()
            for protocol, entries in grouped.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict):
                        tag = entry.get("tag")
                        if not tag:
                            continue
                        inbounds.append(
                            VpnInboundInfo(
                                tag=str(tag),
                                protocol=str(entry.get("protocol") or protocol or ""),
                                network=str(entry.get("network") or ""),
                            )
                        )
            return inbounds
        finally:
            await panel.close()

    async def create_vpn_user(
        self,
        url: str,
        username: str,
        password: str,
        *,
        vpn_username: str,
        group_ids: list[str],
        proxy_settings: dict[str, Any] | None = None,
    ) -> VpnUserInfo:
        panel = RebeccaPanel(url, username, password)
        try:
            await panel.login()
            service_ids: list[int] = []
            for gid in group_ids:
                try:
                    service_ids.append(int(gid))
                except (TypeError, ValueError):
                    continue
            if not service_ids:
                service_ids = await panel.list_all_service_ids()
            payload = NewUserPayload(
                username=vpn_username,
                proxies=dict(proxy_settings or {}),
            )
            if len(service_ids) == 1:
                payload.service_id = service_ids[0]
            elif service_ids:
                payload.service_ids = service_ids
            else:
                inbounds = await panel.fetch_all_inbound_assignment()
                if inbounds:
                    payload.inbounds = inbounds
            try:
                record = await panel.create_user(payload)
            except PanelResponseError as exc:
                detail = (exc.message or "").lower()
                if "exist" in detail or "already" in detail:
                    raise ClientExistsError(exc.message, username=vpn_username) from exc
                raise PanelAdapterError(exc.message) from exc
            return _to_vpn_user_info(record, url)
        finally:
            await panel.close()

    async def get_vpn_user(self, url: str, username: str, password: str, *, vpn_username: str) -> VpnUserInfo:
        panel = RebeccaPanel(url, username, password)
        try:
            await panel.login()
            return _to_vpn_user_info(await panel.get_user(vpn_username), url)
        except ClientNotFoundError:
            raise
        finally:
            await panel.close()

    async def update_vpn_user(
        self,
        url: str,
        username: str,
        password: str,
        *,
        vpn_username: str,
        group_ids: list[str],
    ) -> VpnUserInfo:
        panel = RebeccaPanel(url, username, password)
        try:
            await panel.login()
            await panel.get_user(vpn_username)
            service_ids = [int(g) for g in group_ids if str(g).isdigit()]
            fields: dict[str, Any] = {}
            if len(service_ids) == 1:
                fields["service_id"] = service_ids[0]
            elif service_ids:
                fields["services"] = service_ids
            if fields:
                record = await panel.update_user(vpn_username, **fields)
            else:
                record = await panel.get_user(vpn_username)
            return _to_vpn_user_info(record, url)
        finally:
            await panel.close()

    async def delete_vpn_user(self, url: str, username: str, password: str, *, vpn_username: str) -> bool:
        panel = RebeccaPanel(url, username, password)
        try:
            await panel.login()
            try:
                await panel.get_user(vpn_username)
            except ClientNotFoundError:
                return False
            await panel.delete_user(vpn_username)
            try:
                await panel.get_user(vpn_username)
            except ClientNotFoundError:
                return True
            return False
        finally:
            await panel.close()
