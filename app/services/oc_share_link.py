"""Parse proxy share links for OC subscription-backed hosts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.services.oc_upstream_subscription import _decode_links_payload


@dataclass
class ParsedShareLink:
    raw: str
    protocol: str
    address: str
    port: int | None
    remark: str
    network: str = "tcp"
    security: str | None = None
    sni: list[str] = field(default_factory=list)
    host: list[str] = field(default_factory=list)
    path: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def match_keys(self) -> set[str]:
        keys: set[str] = set()
        for candidate in (self.remark,):
            norm = _normalize_key(candidate)
            if norm:
                keys.add(norm)
        return keys


def _normalize_key(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", "", text)


_CONFIGURATION_SCHEMES = (
    "vless://",
    "vmess://",
    "trojan://",
    "ss://",
    "hysteria2://",
    "hy2://",
    "wireguard://",
)


def is_importable_configuration_link(link: str) -> bool:
    """
    True when a subscription line is a usable proxy share link (not plain/HTML metadata).

    Remark content is not used to reject imports.
    """
    raw = (link or "").strip()
    if not raw or len(raw) > 8192:
        return False
    lower = raw.lower()
    if lower.startswith("<") or "<html" in lower or "<!doctype" in lower:
        return False
    if not any(lower.startswith(scheme) for scheme in _CONFIGURATION_SCHEMES):
        return False
    if parse_share_link(raw) is not None:
        return True
    return lower.startswith(("hysteria2://", "hy2://", "wireguard://"))


def decode_subscription_links(body: str) -> list[str]:
    lines = _decode_links_payload(body or "")
    return [line for line in lines if is_importable_configuration_link(line)]


def subscription_link_uri_fingerprint(link: str) -> str | None:
    """Stable identity for a destination subscription config (URI without fragment)."""
    raw = (link or "").strip()
    if not raw:
        return None
    base = raw.split("#", 1)[0].strip()
    if not base:
        return None
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def destination_virtual_inbound_tag(panel_id: int, destination_config_id: str) -> str:
    return f"oc_{panel_id}_d_{destination_config_id}"


def effective_destination_inbound_tag(
    panel_id: int,
    destination_config_id: str,
    stored_virtual_inbound_tag: str | None,
) -> str:
    """Canonical group/subscription tag for a panel destination row."""
    stored = (stored_virtual_inbound_tag or "").strip()
    if stored and is_oc_destination_inbound_tag(stored):
        return stored
    return destination_virtual_inbound_tag(panel_id, destination_config_id)


def is_oc_destination_inbound_tag(tag: str | None) -> bool:
    """True for ``oc_<panel_id>_d_<destination_config_id>`` virtual host tags."""
    return parse_destination_tag(tag or "") is not None


def parse_destination_tag(tag: str) -> tuple[int, str] | None:
    """Return (panel_id, destination_config_id) for ``oc_<panel>_d_<id>`` tags."""
    if not tag or not tag.startswith("oc_"):
        return None
    parts = tag.split("_", 3)
    if len(parts) != 4 or parts[0] != "oc" or parts[2] != "d" or not parts[1].isdigit():
        return None
    dest_id = parts[3]
    if not dest_id:
        return None
    return int(parts[1]), dest_id


def subscription_config_links(links: list[str]) -> list[ParsedShareLink]:
    """Destination subscription configs (one per importable share link)."""
    out: list[ParsedShareLink] = []
    seen_uri: set[str] = set()
    for link in links:
        if not is_importable_configuration_link(link):
            continue
        parsed = parse_share_link(link)
        if parsed is None:
            parsed = _parse_opaque_share_link(link)
        if parsed is None:
            continue
        fp = subscription_link_uri_fingerprint(parsed.raw)
        if not fp or fp in seen_uri:
            continue
        seen_uri.add(fp)
        out.append(parsed)
    return out


def parse_share_link(link: str) -> ParsedShareLink | None:
    raw = (link or "").strip()
    if not raw:
        return None
    if raw.startswith("vless://"):
        return _parse_vless_trojan(raw, "vless")
    if raw.startswith("trojan://"):
        return _parse_vless_trojan(raw, "trojan")
    if raw.startswith("vmess://"):
        return _parse_vmess(raw)
    if raw.startswith("ss://"):
        return _parse_shadowsocks(raw)
    if raw.startswith("hysteria2://") or raw.startswith("hy2://"):
        return _parse_opaque_share_link(raw)
    if raw.startswith("wireguard://"):
        return _parse_opaque_share_link(raw)
    return None


def _parse_opaque_share_link(raw: str) -> ParsedShareLink | None:
    """Minimal parser for schemes we materialize but do not fully decode."""
    text = (raw or "").strip()
    if not text:
        return None
    if "#" in text:
        base, frag = text.split("#", 1)
        remark = unquote(frag)
    else:
        base, remark = text, ""
    scheme = base.split("://", 1)[0].lower() if "://" in base else ""
    if scheme in ("hysteria2", "hy2"):
        protocol = "hysteria2"
    elif scheme == "wireguard":
        protocol = "wireguard"
    else:
        return None
    return ParsedShareLink(
        raw=text,
        protocol=protocol,
        address="",
        port=None,
        remark=remark,
        network="tcp",
        extra={},
    )


def _parse_vless_trojan(raw: str, protocol: str) -> ParsedShareLink | None:
    try:
        if "#" in raw:
            base, frag = raw.split("#", 1)
            remark = unquote(frag)
        else:
            base, remark = raw, ""
        parsed = urlparse(base)
        address = parsed.hostname or ""
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True)
        flat = {k: (v[0] if v else "") for k, v in query.items()}
        network = (flat.get("type") or flat.get("network") or "tcp").lower()
        security = flat.get("security") or flat.get("tls")
        sni = [flat["sni"]] if flat.get("sni") else []
        host_header = flat.get("host") or ""
        hosts = [h.strip() for h in host_header.split(",") if h.strip()]
        path = flat.get("path") or None
        if path:
            path = unquote(path)
        return ParsedShareLink(
            raw=raw,
            protocol=protocol,
            address=address,
            port=port,
            remark=remark,
            network=network,
            security=security,
            sni=sni,
            host=hosts,
            path=path,
            extra=flat,
        )
    except Exception:
        return None


def _parse_vmess(raw: str) -> ParsedShareLink | None:
    try:
        payload_b64 = raw[len("vmess://") :]
        data = json.loads(base64.b64decode(payload_b64 + "==", validate=False).decode("utf-8", errors="replace"))
        remark = str(data.get("ps") or data.get("remark") or "")
        return ParsedShareLink(
            raw=raw,
            protocol="vmess",
            address=str(data.get("add") or ""),
            port=int(data.get("port") or 0) or None,
            remark=remark,
            network=str(data.get("net") or "tcp").lower(),
            security=str(data.get("tls") or "") or None,
            sni=[str(data.get("sni"))] if data.get("sni") else [],
            host=[str(data.get("host"))] if data.get("host") else [],
            path=str(data.get("path") or "") or None,
            extra=data if isinstance(data, dict) else {},
        )
    except Exception:
        return None


def _parse_shadowsocks(raw: str) -> ParsedShareLink | None:
    try:
        if "#" in raw:
            base, frag = raw.split("#", 1)
            remark = unquote(frag)
        else:
            base, remark = raw, ""
        parsed = urlparse(base)
        address = parsed.hostname or ""
        port = parsed.port
        return ParsedShareLink(
            raw=raw,
            protocol="shadowsocks",
            address=address,
            port=port,
            remark=remark,
            network="tcp",
            extra={},
        )
    except Exception:
        return None


def index_subscription_links(links: list[str]) -> dict[str, ParsedShareLink]:
    """Map normalized remark/config keys to parsed links (first wins)."""
    out: dict[str, ParsedShareLink] = {}
    for link in links:
        parsed = parse_share_link(link)
        if parsed is None:
            continue
        for key in parsed.match_keys():
            out.setdefault(key, parsed)
    return out


def _tokens(value: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (value or "").lower()) if len(t) >= 2}


def _remark_hints(remark: str) -> set[str]:
    """ASCII keywords in subscription remarks (handles stylized Unicode labels)."""
    upper = (remark or "").upper()
    hints: set[str] = set()
    for keyword in (
        "TUNNEL",
        "CDN",
        "DIRECT",
        "XHTTP",
        "HTTP",
        "VLESS",
        "VMESS",
        "SHADOW",
        "WIREGUARD",
        "GRPC",
        "REALITY",
    ):
        if keyword in upper:
            hints.add(keyword.lower())
    if "FR" in upper or "🇫🇷" in remark:
        hints.add("fr")
    if "NL" in upper or "🇳🇱" in remark:
        hints.add("nl")
    return hints


def match_link_for_config(
    index: dict[str, ParsedShareLink],
    *,
    source_config_id: str,
    source_name: str,
    candidates: list[ParsedShareLink] | None = None,
) -> ParsedShareLink | None:
    pool = candidates if candidates is not None else list(index.values())

    for candidate in (source_config_id, source_name):
        key = _normalize_key(candidate)
        if key and key in index:
            return index[key]
    # Fuzzy: remark contains config id substring
    norm_id = _normalize_key(source_config_id)
    if norm_id:
        for parsed in pool:
            if norm_id in _normalize_key(parsed.remark):
                return parsed
    # Token overlap between config name and subscription remark (CDN, XHTTP, etc.)
    config_tokens = _tokens(source_config_id) | _tokens(source_name)
    if not config_tokens:
        return None
    best: ParsedShareLink | None = None
    best_score = 0
    for parsed in pool:
        remark_tokens = _tokens(parsed.remark) | _remark_hints(parsed.remark)
        score = len(config_tokens & remark_tokens)
        # Common abbreviations on destination panels
        remark_norm = _normalize_key(parsed.remark)
        config_norm = _normalize_key(source_config_id)
        if "xhttp" in remark_norm and "xhhtt" in config_norm:
            score += 2
        if "cdn" in remark_norm and ("tcp" in config_norm or "http" in config_norm):
            score += 1
        if "direct" in remark_norm and "shadow" in config_norm:
            score += 2
        if "tunnel" in remark_tokens and config_norm.startswith("out"):
            score += 2
        if "cdn" in remark_tokens and config_norm in ("direct", "out"):
            score += 1
        if "fr" in remark_tokens and config_norm.startswith("out"):
            score += 1
        if "nl" in remark_tokens and "direct" in config_norm:
            score += 2
        if score > best_score:
            best_score = score
            best = parsed
    return best if best_score > 0 else None


def match_upstream_link_for_oc_source_config(
    raw_links: list[str],
    oc_source_config_id: str,
    *,
    source_name: str | None = None,
) -> str | None:
    """
    Resolve a customer subscription link for a stable OC catalog config id.

    Does not use discovery URI fingerprints (customer UUID/URI may differ).
    """
    config_id = (oc_source_config_id or "").strip()
    if not config_id or not raw_links:
        return None
    catalog_name = (source_name or config_id).strip()
    pool = subscription_config_links(raw_links)
    if not pool:
        return None

    exact_keys = {k for k in (_normalize_key(config_id), _normalize_key(catalog_name)) if k}
    exact_hits = [p for p in pool if _normalize_key(p.remark) in exact_keys]
    if len(exact_hits) > 1:
        return None
    if len(exact_hits) == 1:
        return exact_hits[0].raw

    assigned = match_links_to_configs(raw_links, [(config_id, catalog_name)])
    parsed = assigned.get(config_id)
    if parsed is None:
        return None
    norm_remark = _normalize_key(parsed.remark)
    if norm_remark:
        dupes = [p for p in pool if _normalize_key(p.remark) == norm_remark]
        if len(dupes) > 1:
            identities = {
                vless_trojan_client_uuid(p.raw) or p.raw for p in dupes
            }
            if len(identities) > 1:
                return None
    return parsed.raw


def match_links_to_configs(
    links: list[str],
    configs: list[tuple[str, str]],
) -> dict[str, ParsedShareLink]:
    """Map source_config_id -> parsed link (one link per config, no reuse)."""
    parsed_links = []
    for link in links:
        if not is_importable_configuration_link(link):
            continue
        p = parse_share_link(link) or _parse_opaque_share_link(link)
        if p is not None:
            parsed_links.append(p)
    index = index_subscription_links(links)
    assigned: dict[str, ParsedShareLink] = {}
    used: set[str] = set()

    for config_id, config_name in configs:
        available = [p for p in parsed_links if p.raw not in used]
        match = match_link_for_config(
            index,
            source_config_id=config_id,
            source_name=config_name,
            candidates=available,
        )
        if match is not None:
            assigned[config_id] = match
            used.add(match.raw)
    return assigned


def share_link_config_base(link: str) -> str:
    """Share link URI without the ``#remark`` fragment (config identity for vless/trojan/ss)."""
    return (link or "").split("#", 1)[0]


def vless_trojan_client_uuid(link: str) -> str | None:
    """Client UUID/user id embedded in a vless/trojan share link (not an OC internal tag)."""
    parsed = urlparse(share_link_config_base(link))
    if parsed.scheme not in ("vless", "trojan") or not parsed.username:
        return None
    return unquote(parsed.username)


def replace_link_remark(link: str, remark: str) -> str:
    from urllib.parse import quote

    base = share_link_config_base(link)
    return f"{base}#{quote(remark)}"
