"""Fetch and merge seller-panel subscription bodies for OC-backed users."""

from __future__ import annotations

import base64
import ipaddress
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import yaml
from aiohttp import ClientResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCUserMapping, OCPanel
from app.node.oc_sync import OC_PANEL_BLOCKING_STATUSES
from app.services.oc_subscription_runtime import load_runtime_oc_subscription_state, resolve_user_tenant_id
from app.services.oc_user_mapping_state import oc_user_mapping_runtime_active_criteria
from app.utils.http_client import create_outbound_http_session
from app.utils.logger import get_logger
from app.utils.reality_scan import RealityScanError, resolve_public_ip

logger = get_logger("oc-upstream-sub")

MAX_SUBSCRIPTION_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 12.0
MAX_REDIRECTS = 3

# Formats where upstream fetch is supported (wireguard zip merge is not attempted).
_SUPPORTED_FORMATS = frozenset(
    {
        "links",
        "links_base64",
        "xray",
        "sing_box",
        "clash",
        "clash_meta",
        "outline",
    }
)


@dataclass(frozen=True)
class OcUpstreamSource:
    panel_id: int
    subscription_url: str


def _address_is_public(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(ip_obj.is_global) and not (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _host_resolves_to_public(host: str) -> bool:
    if not host:
        return False
    try:
        ip_obj = ipaddress.ip_address(host)
        return _address_is_public(ip_obj)
    except ValueError:
        pass
    try:
        resolve_public_ip(host)
        return True
    except RealityScanError:
        return False


def validate_upstream_subscription_url(url: str) -> str:
    """Return normalized HTTPS URL or raise ValueError."""
    raw = (url or "").strip()
    if not raw or len(raw) > 2048:
        raise ValueError("invalid subscription url length")
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise ValueError("subscription url must use https")
    if not parsed.hostname:
        raise ValueError("subscription url missing host")
    if parsed.username or parsed.password:
        raise ValueError("subscription url must not embed credentials")
    if not _host_resolves_to_public(parsed.hostname):
        raise ValueError("subscription url host is not a public address")
    path = parsed.path or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def build_upstream_format_url(base_url: str, config_format: str) -> str:
    normalized = base_url.rstrip("/")
    if config_format == "links":
        return f"{normalized}/links"
    if config_format == "links_base64":
        return f"{normalized}/links_base64"
    return f"{normalized}/{config_format}"


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return pa.scheme == pb.scheme and pa.netloc.lower() == pb.netloc.lower()


async def _read_limited_body(resp: ClientResponse) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        block = await resp.content.read(65536)
        if not block:
            break
        total += len(block)
        if total > MAX_SUBSCRIPTION_BYTES:
            raise ValueError("upstream subscription response too large")
        chunks.append(block)
    return b"".join(chunks)


async def fetch_upstream_subscription_body(
    subscription_url: str,
    config_format: str,
    *,
    user_agent: str = "",
) -> str | bytes:
    """Server-side fetch of seller subscription for the requested central format."""
    if config_format not in _SUPPORTED_FORMATS:
        raise ValueError(f"unsupported upstream format {config_format}")

    validated_base = validate_upstream_subscription_url(subscription_url)
    fetch_url = build_upstream_format_url(validated_base, config_format)
    headers = {}
    if user_agent:
        headers["User-Agent"] = user_agent

    current_url = fetch_url
    async with create_outbound_http_session(timeout_seconds=FETCH_TIMEOUT_SECONDS) as session:
        for _ in range(MAX_REDIRECTS + 1):
            async with session.get(current_url, headers=headers, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise ValueError("redirect without location")
                    next_url = urljoin(current_url, location)
                    if not _same_origin(validated_base, next_url):
                        raise ValueError("redirect to foreign host")
                    validate_upstream_subscription_url(next_url)
                    current_url = next_url
                    continue
                if resp.status >= 400:
                    raise ValueError(f"upstream http {resp.status}")
                body = await _read_limited_body(resp)
                if config_format == "wireguard":
                    return body
                return body.decode("utf-8", errors="replace")


def _split_share_links(text: str) -> list[str]:
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return lines


def _decode_links_payload(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("vmess://") or stripped.startswith("vless://") or stripped.startswith("trojan://"):
        return _split_share_links(stripped)
    try:
        decoded = base64.b64decode(stripped, validate=False).decode("utf-8", errors="replace")
        return _split_share_links(decoded)
    except Exception:
        return _split_share_links(stripped)


def _encode_links_base64(links: list[str]) -> str:
    payload = "\n".join(links)
    return base64.b64encode(payload.encode()).decode()


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def merge_subscription_payloads(
    native: str | bytes,
    upstream_parts: list[str | bytes],
    config_format: str,
    *,
    as_base64: bool,
) -> str | bytes:
    if not upstream_parts:
        return native
    if config_format == "wireguard":
        return native

    if config_format in ("links", "links_base64"):
        native_text = native if isinstance(native, str) else native.decode("utf-8", errors="replace")
        links = _decode_links_payload(native_text) if config_format == "links_base64" or as_base64 else _split_share_links(
            native_text
        )
        for part in upstream_parts:
            part_text = part if isinstance(part, str) else part.decode("utf-8", errors="replace")
            links.extend(_decode_links_payload(part_text) if config_format == "links_base64" else _split_share_links(part_text))
        merged = _dedupe_preserve_order(links)
        if config_format == "links_base64" or as_base64:
            return _encode_links_base64(merged)
        return "\n".join(merged)

    if config_format == "xray":
        native_text = native if isinstance(native, str) else native.decode("utf-8", errors="replace")
        try:
            merged_list: list[Any] = json.loads(native_text) if native_text.strip() else []
        except json.JSONDecodeError:
            merged_list = []
        if not isinstance(merged_list, list):
            merged_list = [merged_list]
        for part in upstream_parts:
            part_text = part if isinstance(part, str) else part.decode("utf-8", errors="replace")
            try:
                data = json.loads(part_text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                merged_list.extend(data)
            elif data:
                merged_list.append(data)
        return json.dumps(merged_list, ensure_ascii=False, separators=(",", ":"))

    if config_format == "sing_box":
        native_text = native if isinstance(native, str) else native.decode("utf-8", errors="replace")
        try:
            base_doc = json.loads(native_text) if native_text.strip() else {}
        except json.JSONDecodeError:
            base_doc = {}
        if not isinstance(base_doc, dict):
            base_doc = {}
        base_doc.setdefault("outbounds", [])
        base_doc.setdefault("endpoints", [])
        for part in upstream_parts:
            part_text = part if isinstance(part, str) else part.decode("utf-8", errors="replace")
            try:
                doc = json.loads(part_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(doc, dict):
                continue
            base_doc["outbounds"].extend(doc.get("outbounds") or [])
            base_doc["endpoints"].extend(doc.get("endpoints") or [])
        return json.dumps(base_doc, ensure_ascii=False, separators=(",", ":"))

    if config_format in ("clash", "clash_meta"):
        native_text = native if isinstance(native, str) else native.decode("utf-8", errors="replace")
        try:
            base_doc = yaml.safe_load(native_text) or {}
        except yaml.YAMLError:
            base_doc = {}
        if not isinstance(base_doc, dict):
            base_doc = {}
        base_doc.setdefault("proxies", [])
        for part in upstream_parts:
            part_text = part if isinstance(part, str) else part.decode("utf-8", errors="replace")
            try:
                doc = yaml.safe_load(part_text) or {}
            except yaml.YAMLError:
                continue
            if isinstance(doc, dict) and isinstance(doc.get("proxies"), list):
                base_doc["proxies"].extend(doc["proxies"])
        return yaml.safe_dump(base_doc, allow_unicode=True, sort_keys=False)

    if config_format == "outline":
        native_text = native if isinstance(native, str) else native.decode("utf-8", errors="replace")
        try:
            base_doc = json.loads(native_text) if native_text.strip() else {}
        except json.JSONDecodeError:
            base_doc = {}
        if not isinstance(base_doc, dict):
            base_doc = {}
        for part in upstream_parts:
            part_text = part if isinstance(part, str) else part.decode("utf-8", errors="replace")
            try:
                doc = json.loads(part_text)
            except json.JSONDecodeError:
                continue
            if isinstance(doc, dict):
                base_doc.update(doc)
        return json.dumps(base_doc, ensure_ascii=False, separators=(",", ":"))

    return native


async def load_oc_upstream_sources(
    db: AsyncSession, user_id: int, user_tenant_id: int | None
) -> tuple[set[int], list[OcUpstreamSource]]:
    """
    Panels that should use seller subscription bodies instead of synthetic OC hosts.
    """
    allowed_panels, _ = await load_runtime_oc_subscription_state(db, user_id, user_tenant_id)
    if not allowed_panels:
        return set(), []

    panel_rows = (
        await db.execute(
            select(OCPanel.id, OCPanel.sync_status).where(OCPanel.id.in_(allowed_panels))
        )
    ).all()
    active_panels = {
        pid
        for pid, sync_status in panel_rows
        if (sync_status or "") not in OC_PANEL_BLOCKING_STATUSES
    }
    if not active_panels:
        return set(), []

    mappings = (
        await db.execute(
            select(OCUserMapping).where(
                OCUserMapping.user_id == user_id,
                OCUserMapping.panel_id.in_(active_panels),
                oc_user_mapping_runtime_active_criteria(),
            )
        )
    ).scalars().all()

    sources: list[OcUpstreamSource] = []
    upstream_panel_ids: set[int] = set()
    for mapping in mappings:
        url = (mapping.external_subscription_url or "").strip()
        if not url:
            continue
        try:
            validate_upstream_subscription_url(url)
        except ValueError:
            logger.warning(
                "oc_upstream_invalid_url panel_id=%s user_id=%s",
                mapping.panel_id,
                user_id,
            )
            continue
        upstream_panel_ids.add(mapping.panel_id)
        sources.append(OcUpstreamSource(panel_id=mapping.panel_id, subscription_url=url))

    return upstream_panel_ids, sources


async def append_oc_upstream_subscriptions(
    native: str | bytes,
    user_id: int,
    admin_id: int | None,
    config_format: str,
    *,
    as_base64: bool,
    user_agent: str = "",
) -> str | bytes:
    from app.db import GetDB

    if config_format not in _SUPPORTED_FORMATS:
        return native

    async with GetDB() as db:
        tenant_id = await resolve_user_tenant_id(db, user_id, admin_id)
        _, sources = await load_oc_upstream_sources(db, user_id, tenant_id)

    if not sources:
        return native

    upstream_bodies: list[str | bytes] = []
    for source in sources:
        try:
            logger.info(
                "oc_upstream_fetch_start panel_id=%s user_id=%s format=%s",
                source.panel_id,
                user_id,
                config_format,
            )
            body = await fetch_upstream_subscription_body(
                source.subscription_url,
                config_format,
                user_agent=user_agent,
            )
            if isinstance(body, str):
                link_count = len(_decode_links_payload(body)) if config_format in ("links", "links_base64") else 0
            else:
                link_count = 0
            logger.info(
                "oc_upstream_fetch_ok panel_id=%s user_id=%s bytes=%s links=%s",
                source.panel_id,
                user_id,
                len(body) if body else 0,
                link_count,
            )
            if body:
                upstream_bodies.append(body)
        except Exception as exc:
            logger.warning(
                "oc_upstream_fetch_failed panel_id=%s user_id=%s error=%s",
                source.panel_id,
                user_id,
                type(exc).__name__,
            )
            continue

    if not upstream_bodies:
        return native

    return merge_subscription_payloads(native, upstream_bodies, config_format, as_base64=as_base64)
