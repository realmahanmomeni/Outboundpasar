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

    fetch_candidates: list[str] = [fetch_url]
    if config_format in ("links", "links_base64") and fetch_url != validated_base:
        fetch_candidates.append(validated_base)

    async with create_outbound_http_session(timeout_seconds=FETCH_TIMEOUT_SECONDS) as session:
        last_status: int | None = None
        for candidate_index, current_url in enumerate(fetch_candidates):
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
                    if resp.status == 404 and candidate_index + 1 < len(fetch_candidates):
                        last_status = resp.status
                        break
                    if resp.status >= 400:
                        raise ValueError(f"upstream http {resp.status}")
                    body = await _read_limited_body(resp)
                    if config_format == "wireguard":
                        return body
                    return body.decode("utf-8", errors="replace")
            else:
                continue
            continue
        if last_status is not None:
            raise ValueError(f"upstream http {last_status}")
        raise ValueError("upstream fetch failed")


def _split_share_links(text: str) -> list[str]:
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        # A BOM / zero-width mark in front of a scheme would make the link unparseable and the
        # client would silently lose that config.
        stripped = line.strip().lstrip("\ufeff\u200b\u200c\u200d\u200e\u200f").strip()
        if stripped:
            lines.append(stripped)
    return lines


_PLAINTEXT_LINK_PREFIXES = (
    "vmess://",
    "vless://",
    "trojan://",
    "ss://",
    "hysteria2://",
    "hy2://",
    "wireguard://",
)


def _body_has_leading_plaintext_share_links(text: str) -> bool:
    """True when the payload is newline-separated share links (not a single base64 blob)."""
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip().lower()
        if not stripped:
            continue
        return any(stripped.startswith(prefix) for prefix in _PLAINTEXT_LINK_PREFIXES)
    return False


def _b64decode_links(value: str) -> str | None:
    """Decode a base64 links body: standard or URL-safe alphabet, padded or not, whitespace tolerant."""
    import re

    cleaned = re.sub(r"\s+", "", value or "")
    if not cleaned or not re.fullmatch(r"[A-Za-z0-9+/_\-]+={0,2}", cleaned):
        return None
    cleaned = cleaned.rstrip("=").replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned, validate=True).decode("utf-8", errors="replace")
    except Exception:
        return None


def _decode_links_payload(text: str) -> list[str]:
    stripped = (text or "").lstrip("\ufeff").strip()
    if not stripped:
        return []
    if _body_has_leading_plaintext_share_links(stripped):
        return _split_share_links(stripped)
    decoded = _b64decode_links(stripped)
    if decoded is not None:
        decoded_lines = _split_share_links(decoded)
        if any(line.lower().startswith(_PLAINTEXT_LINK_PREFIXES) for line in decoded_lines):
            return decoded_lines
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


@dataclass(frozen=True)
class _VlessEndpointCredential:
    """Stable vless identity: user id + host + port (not remark-based)."""

    user_id: str
    host: str
    port: int


def _allowed_vless_credentials(allowed_links: list[str]) -> frozenset[_VlessEndpointCredential]:
    from urllib.parse import unquote, urlparse

    from app.services.oc_share_link import parse_share_link

    creds: set[_VlessEndpointCredential] = set()
    for link in allowed_links:
        raw = (link or "").strip()
        if not raw:
            continue
        parsed = parse_share_link(raw)
        if parsed is None or parsed.protocol != "vless" or not parsed.address or parsed.port is None:
            parsed_uri = urlparse(raw.split("#", 1)[0])
            if parsed_uri.scheme != "vless" or not parsed_uri.hostname or parsed_uri.port is None:
                continue
            user_id = unquote(parsed_uri.username or "")
            host = parsed_uri.hostname.lower()
            port = int(parsed_uri.port)
        else:
            parsed_uri = urlparse(raw.split("#", 1)[0])
            user_id = unquote(parsed_uri.username or "")
            host = parsed.address.lower()
            port = int(parsed.port)
        if user_id and host and port > 0:
            creds.add(_VlessEndpointCredential(user_id=user_id, host=host, port=port))
    return frozenset(creds)


def _allowed_link_config_bases(allowed_links: list[str]) -> frozenset[str]:
    from app.services.oc_share_link import share_link_config_base

    return frozenset(
        share_link_config_base(link)
        for link in allowed_links
        if (link or "").strip()
    )


def _vless_cred_from_clash_proxy(proxy: dict) -> _VlessEndpointCredential | None:
    if not isinstance(proxy, dict) or str(proxy.get("type") or "").lower() != "vless":
        return None
    user_id = str(proxy.get("uuid") or "").strip()
    host = str(proxy.get("server") or "").strip().lower()
    port = proxy.get("port")
    if not user_id or not host or port is None:
        return None
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return None
    return _VlessEndpointCredential(user_id=user_id, host=host, port=port_i)


def _vless_cred_from_xray_item(item: dict) -> _VlessEndpointCredential | None:
    if not isinstance(item, dict):
        return None
    if str(item.get("protocol") or item.get("type") or "").lower() != "vless":
        return None
    user_id = str(item.get("id") or item.get("uuid") or "").strip()
    host = str(item.get("add") or item.get("address") or "").strip().lower()
    port = item.get("port")
    if not user_id or not host or port is None:
        return None
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return None
    return _VlessEndpointCredential(user_id=user_id, host=host, port=port_i)


def _vless_cred_from_singbox_outbound(outbound: dict) -> _VlessEndpointCredential | None:
    if not isinstance(outbound, dict):
        return None
    if str(outbound.get("type") or "").lower() != "vless":
        return None
    user_id = str(outbound.get("uuid") or outbound.get("id") or "").strip()
    host = str(outbound.get("server") or outbound.get("address") or "").strip().lower()
    port = outbound.get("server_port") or outbound.get("port")
    if not user_id or not host or port is None:
        return None
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return None
    return _VlessEndpointCredential(user_id=user_id, host=host, port=port_i)


def _clash_proxy_share_link_for_filter(proxy: dict) -> str | None:
    """Build a share link (no remark) when clash fields are sufficient for exact config-base matching."""
    if not isinstance(proxy, dict):
        return None
    proxy_type = str(proxy.get("type") or "").lower()
    server = proxy.get("server")
    port = proxy.get("port")
    if not server or port is None:
        return None
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return None
    from urllib.parse import quote

    if proxy_type == "vless":
        user_id = str(proxy.get("uuid") or "").strip()
        if not user_id:
            return None
        return f"vless://{quote(user_id, safe='')}@{server}:{port_i}?encryption=none#"
    if proxy_type == "trojan":
        password = proxy.get("password")
        if password is None or str(password) == "":
            return None
        return f"trojan://{quote(str(password), safe='')}@{server}:{port_i}#"
    if proxy_type == "ss":
        cipher = proxy.get("cipher")
        password = proxy.get("password")
        if not cipher or password is None:
            return None
        userinfo = base64.urlsafe_b64encode(f"{cipher}:{password}".encode()).decode().rstrip("=")
        return f"ss://{userinfo}@{server}:{port_i}#"
    return None


def _proxy_dict_allowed(
    proxy: dict,
    allowed_vless: frozenset[_VlessEndpointCredential],
    allowed_bases: frozenset[str],
) -> bool:
    from app.services.oc_share_link import share_link_config_base

    cred = _vless_cred_from_clash_proxy(proxy)
    if cred is not None and cred in allowed_vless:
        return True
    synthesized = _clash_proxy_share_link_for_filter(proxy)
    if synthesized and share_link_config_base(synthesized) in allowed_bases:
        return True
    return False


def _xray_item_allowed(
    item: Any,
    allowed_vless: frozenset[_VlessEndpointCredential],
    allowed_bases: frozenset[str],
) -> bool:
    from app.services.oc_share_link import share_link_config_base

    if not isinstance(item, dict):
        return False
    cred = _vless_cred_from_xray_item(item)
    if cred is not None and cred in allowed_vless:
        return True
    protocol = str(item.get("protocol") or item.get("type") or "").lower()
    server = item.get("add") or item.get("address")
    port = item.get("port")
    if not server or port is None:
        return False
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return False
    from urllib.parse import quote

    if protocol == "trojan":
        password = item.get("password")
        if password is None or str(password) == "":
            return False
        link = f"trojan://{quote(str(password), safe='')}@{server}:{port_i}#"
        return share_link_config_base(link) in allowed_bases
    if protocol in ("shadowsocks", "ss"):
        method = item.get("method")
        password = item.get("password")
        if not method or password is None:
            return False
        userinfo = base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
        link = f"ss://{userinfo}@{server}:{port_i}#"
        return share_link_config_base(link) in allowed_bases
    return False


def _singbox_outbound_allowed(
    outbound: Any,
    allowed_vless: frozenset[_VlessEndpointCredential],
    allowed_bases: frozenset[str],
) -> bool:
    from app.services.oc_share_link import share_link_config_base

    if not isinstance(outbound, dict):
        return False
    cred = _vless_cred_from_singbox_outbound(outbound)
    if cred is not None and cred in allowed_vless:
        return True
    outbound_type = str(outbound.get("type") or "").lower()
    server = outbound.get("server") or outbound.get("address")
    port = outbound.get("server_port") or outbound.get("port")
    if not server or port is None:
        return False
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return False
    from urllib.parse import quote

    if outbound_type == "trojan":
        password = outbound.get("password")
        if password is None or str(password) == "":
            return False
        link = f"trojan://{quote(str(password), safe='')}@{server}:{port_i}#"
        return share_link_config_base(link) in allowed_bases
    if outbound_type in ("shadowsocks", "ss"):
        method = outbound.get("method")
        password = outbound.get("password")
        if not method or password is None:
            return False
        userinfo = base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
        link = f"ss://{userinfo}@{server}:{port_i}#"
        return share_link_config_base(link) in allowed_bases
    return False


def filter_upstream_subscription_body(
    body: str | bytes,
    config_format: str,
    allowed_links: list[str],
) -> str | bytes:
    """
    Drop upstream subscription entries that are not part of the user's group-selected OC hosts.

    When ``allowed_links`` is empty, returns an empty payload for the format (never pass-through).
    """
    if config_format == "wireguard":
        # Panel WireGuard archives cannot be mapped to group-selected share links; never pass through.
        return b""
    if not allowed_links:
        if config_format in ("links", "links_base64"):
            return ""
        if config_format in ("clash", "clash_meta"):
            return "proxies: []\n"
        if config_format == "xray":
            return "[]"
        if config_format == "sing_box":
            return json.dumps({"outbounds": [], "endpoints": []}, ensure_ascii=False, separators=(",", ":"))
        if config_format == "outline":
            return "{}"
        if config_format == "wireguard":
            return b""
        return ""

    allowed_bases = _allowed_link_config_bases(allowed_links)
    allowed_vless = _allowed_vless_credentials(allowed_links)
    text = body if isinstance(body, str) else body.decode("utf-8", errors="replace")

    if config_format in ("links", "links_base64"):
        from app.services.oc_share_link import share_link_config_base

        decoded = _decode_links_payload(text)
        kept = [link for link in decoded if share_link_config_base(link) in allowed_bases]
        if config_format == "links_base64":
            return _encode_links_base64(kept)
        return "\n".join(kept)

    if config_format in ("clash", "clash_meta"):
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        proxies = doc.get("proxies") or []
        if isinstance(proxies, list):
            doc["proxies"] = [
                p for p in proxies if _proxy_dict_allowed(p, allowed_vless, allowed_bases)
            ]
        return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)

    if config_format == "xray":
        try:
            data = json.loads(text) if text.strip() else []
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            filtered = [item for item in data if _xray_item_allowed(item, allowed_vless, allowed_bases)]
            return json.dumps(filtered, ensure_ascii=False, separators=(",", ":"))
        return "[]"

    if config_format == "sing_box":
        try:
            doc = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        outbounds = doc.get("outbounds") or []
        endpoints = doc.get("endpoints") or []
        if isinstance(outbounds, list):
            doc["outbounds"] = [
                o for o in outbounds if _singbox_outbound_allowed(o, allowed_vless, allowed_bases)
            ]
        if isinstance(endpoints, list):
            doc["endpoints"] = [
                e for e in endpoints if _singbox_outbound_allowed(e, allowed_vless, allowed_bases)
            ]
        return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))

    if config_format == "outline":
        # Outline seller payloads cannot be mapped to group-selected share links; never merge unfiltered.
        return "{}"

    return b"" if isinstance(body, bytes) else ""


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
    oc_inbound_tags: list[str] | None = None,
    format_variables: dict | None = None,
) -> str | bytes:
    """
    Legacy seller-body merge (unrestricted users only).

    Users with runtime-active OC panels are served exclusively from their authorized share links,
    converted through the shared serializers inside ``process_inbounds_and_tags``.  Seller
    structured bodies are never fetched or merged for them: they could only contribute entries
    outside the user's group selection (or, after filtering, duplicates of the authorized set).
    """
    from app.db import GetDB

    if config_format not in _SUPPORTED_FORMATS:
        return native

    async with GetDB() as db:
        tenant_id = await resolve_user_tenant_id(db, user_id, admin_id)
        allowed_panels, _ = await load_runtime_oc_subscription_state(db, user_id, tenant_id)
        if allowed_panels:
            return native
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
