"""Local Rebecca integration API (mirrors Outbound Center integration_service VPN ops)."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.services.panel_drivers.exceptions import (
    ClientExistsError,
    ClientNotFoundError,
    InsufficientPermissionsError,
    PanelAdapterError,
    PanelAuthError,
    PanelConnectionError,
    PanelResponseError,
)
from app.services.panel_drivers.rebecca.vpn_driver import RebeccaVpnDriver
from app.services.panel_drivers.types import VpnGroupInfo, VpnUserInfo
from app.services.oc_panel_buyer_auth import BuyerPanelAuth

logger = logging.getLogger(__name__)

DISCOVERY_EXTERNAL_USER_ID = "__pasarguard_discovery__"
_DRIVER = RebeccaVpnDriver()


class LocalIntegrationError(Exception):
    def __init__(self, http_status: int, detail: str, *, code: str | None = None):
        self.http_status = http_status
        self.detail = detail
        self.code = code
        super().__init__(detail)


def panel_username_for(account_id: int, subscription_id: int, external_user_id: str) -> str:
    digest = hashlib.sha256(f"pgint:{account_id}:{subscription_id}:{external_user_id}".encode()).hexdigest()[:16]
    return f"pg{subscription_id}_{digest}"


def _translate_error(exc: Exception) -> LocalIntegrationError:
    if isinstance(exc, ClientNotFoundError):
        return LocalIntegrationError(404, "USER_NOT_FOUND", code="USER_NOT_FOUND")
    if isinstance(exc, ClientExistsError):
        return LocalIntegrationError(409, "USER_EXISTS", code="USER_EXISTS")
    if isinstance(exc, PanelConnectionError):
        return LocalIntegrationError(502, "PANEL_UNAVAILABLE", code="PANEL_UNAVAILABLE")
    if isinstance(exc, PanelAuthError):
        return LocalIntegrationError(502, "PANEL_AUTH_FAILED", code="PANEL_AUTH_FAILED")
    if isinstance(exc, (InsufficientPermissionsError, PanelAdapterError)):
        return LocalIntegrationError(502, "PANEL_FORBIDDEN", code="PANEL_FORBIDDEN")
    if isinstance(exc, PanelResponseError):
        logger.warning("rebecca_local_upstream_error: %s", exc.message)
        return LocalIntegrationError(502, "UPSTREAM_ERROR", code="UPSTREAM_ERROR")
    logger.exception("rebecca_local_unexpected_error: %s", exc)
    return LocalIntegrationError(502, "UPSTREAM_ERROR", code="UPSTREAM_ERROR")


def _user_payload(
    external_user_id: str,
    info: VpnUserInfo,
    groups: list[VpnGroupInfo],
) -> dict[str, Any]:
    by_id = {g.id: g for g in groups}
    configs: list[str] = []
    for gid in info.group_ids:
        group = by_id.get(gid)
        if group:
            configs.extend(t for t in group.inbound_tags if t not in configs)
    return {
        "external_user_id": external_user_id,
        "status": info.status or "active",
        "groups": list(info.group_ids),
        "configs": configs,
        "subscription_url": info.subscription_url,
    }


async def _groups(auth: BuyerPanelAuth) -> list[VpnGroupInfo]:
    try:
        return await _DRIVER.list_vpn_groups(auth.panel_url, auth.username, auth.password)
    except Exception as exc:
        raise _translate_error(exc) from exc


async def get_panel_groups(auth: BuyerPanelAuth) -> list[dict[str, str]]:
    return [{"id": g.id, "name": g.name} for g in await _groups(auth)]


async def get_panel_configs(auth: BuyerPanelAuth) -> list[dict[str, Any]]:
    groups = await _groups(auth)
    try:
        inbounds = {i.tag: i for i in await _DRIVER.list_vpn_inbounds(auth.panel_url, auth.username, auth.password)}
    except Exception as exc:
        raise _translate_error(exc) from exc
    tags_from_groups: dict[str, list[str]] = {}
    for group in groups:
        for tag in group.inbound_tags:
            tags_from_groups.setdefault(tag, []).append(group.id)
    configs: list[dict[str, Any]] = []
    for tag in sorted(set(inbounds) | set(tags_from_groups)):
        info = inbounds.get(tag)
        configs.append(
            {
                "id": tag,
                "name": tag,
                "protocol": (info.protocol if info else "") or None,
                "network": (info.network if info else "") or None,
                "group_mapping": {"supported": True, "groups": tags_from_groups.get(tag, [])},
            }
        )
    return configs


async def _resolve_effective_groups(
    auth: BuyerPanelAuth,
    requested_groups: list[str],
    requested_configs: list[str],
) -> tuple[list[str], list[VpnGroupInfo]]:
    groups = await _groups(auth)
    allowed = {g.id: g for g in groups}
    unknown_groups = [g for g in requested_groups if str(g) not in allowed]
    if unknown_groups:
        raise LocalIntegrationError(
            400,
            f"GROUP_NOT_ALLOWED: {unknown_groups}",
            code="GROUP_NOT_ALLOWED",
        )
    effective = [str(g) for g in requested_groups]
    if requested_configs:
        known_tags = {t for g in groups for t in g.inbound_tags}
        unknown_cfg = [c for c in requested_configs if c not in known_tags]
        if unknown_cfg:
            raise LocalIntegrationError(
                400,
                f"CONFIG_NOT_ALLOWED: {unknown_cfg}",
                code="CONFIG_NOT_ALLOWED",
            )
        if not effective:
            for g in groups:
                if set(g.inbound_tags) & set(requested_configs):
                    effective.append(g.id)
        else:
            covered = {t for gid in effective for t in allowed[gid].inbound_tags}
            missing = [c for c in requested_configs if c not in covered]
            if missing:
                raise LocalIntegrationError(
                    400,
                    f"CONFIG_NOT_IN_GROUPS: {missing}",
                    code="CONFIG_NOT_IN_GROUPS",
                )
    return list(dict.fromkeys(effective)), groups


async def provision_external_user(
    auth: BuyerPanelAuth,
    *,
    account_id: int,
    subscription_id: int,
    external_user_id: str,
    groups: list[str],
    configs: list[str],
    proxy_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    vpn_username = panel_username_for(account_id, subscription_id, external_user_id)
    effective, all_groups = await _resolve_effective_groups(auth, groups, configs)
    try:
        try:
            info = await _DRIVER.create_vpn_user(
                auth.panel_url,
                auth.username,
                auth.password,
                vpn_username=vpn_username,
                group_ids=effective,
                proxy_settings=proxy_settings,
            )
        except ClientExistsError:
            info = await _DRIVER.update_vpn_user(
                auth.panel_url,
                auth.username,
                auth.password,
                vpn_username=vpn_username,
                group_ids=effective,
            )
    except Exception as exc:
        raise _translate_error(exc) from exc
    return _user_payload(external_user_id, info, all_groups)


async def get_external_user(
    auth: BuyerPanelAuth,
    *,
    account_id: int,
    subscription_id: int,
    external_user_id: str,
) -> dict[str, Any]:
    vpn_username = panel_username_for(account_id, subscription_id, external_user_id)
    try:
        info = await _DRIVER.get_vpn_user(
            auth.panel_url, auth.username, auth.password, vpn_username=vpn_username
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    groups = await _groups(auth)
    return _user_payload(external_user_id, info, groups)


async def provision_test_user(
    auth: BuyerPanelAuth,
    *,
    account_id: int,
    subscription_id: int,
) -> str:
    await provision_external_user(
        auth,
        account_id=account_id,
        subscription_id=subscription_id,
        external_user_id=DISCOVERY_EXTERNAL_USER_ID,
        groups=[],
        configs=[],
    )
    return f"pasarguard_discovery_{subscription_id}"


async def delete_external_user(
    auth: BuyerPanelAuth,
    *,
    account_id: int,
    subscription_id: int,
    external_user_id: str,
) -> bool:
    vpn_username = panel_username_for(account_id, subscription_id, external_user_id)
    try:
        return await _DRIVER.delete_vpn_user(
            auth.panel_url, auth.username, auth.password, vpn_username=vpn_username
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
