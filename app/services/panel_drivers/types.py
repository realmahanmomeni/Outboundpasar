"""Normalized panel VPN types for PasarGuard-local integration adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VpnGroupInfo:
    id: str
    name: str
    inbound_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class VpnInboundInfo:
    tag: str
    protocol: str = ""
    network: str = ""


@dataclass(frozen=True)
class VpnUserInfo:
    username: str
    status: str = ""
    group_ids: tuple[str, ...] = ()
    subscription_url: str | None = None
    used_bytes: int = 0
    data_limit: int | None = None
