"""Identity extraction for generated subscription bodies (links / Xray / sing-box / Clash).

Every extractor returns ``(protocol, host, port, credential_fingerprint)`` tuples where the last
element is a short, non-reversible SHA-256 prefix, so assertions and failure diffs never contain a
raw credential.  Extractors read the *native* output schema of each format.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from app.services.oc_share_link import parse_share_link, share_link_config_base

Identity = tuple[str, str, int, str]


def fp(secret: str) -> str:
    """Stable non-reversible fingerprint for diagnostics (never log the secret)."""
    return hashlib.sha256((secret or "").encode()).hexdigest()[:12]


def _b64(value: str) -> bytes:
    cleaned = value.replace("-", "+").replace("_", "/").rstrip("=")
    return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4))


def link_identities(links: list[str]) -> list[Identity]:
    """Identities in input order (a list, so duplicates and ordering stay observable)."""
    out: list[Identity] = []
    for link in links:
        raw = (link or "").strip()
        if not raw:
            continue
        parsed = parse_share_link(raw)
        if parsed is None:
            continue
        base = share_link_config_base(raw)
        proto = parsed.protocol
        cred = ""
        host = (parsed.address or "").lower()
        port = parsed.port or 0
        if proto in ("vless", "trojan"):
            netloc = urlsplit(base).netloc
            cred = unquote(netloc.rpartition("@")[0]) if "@" in netloc else ""
        elif proto == "vmess":
            cred = str(parsed.extra.get("id") or "")
        elif proto == "shadowsocks":
            netloc = urlsplit(base).netloc
            userinfo = unquote(netloc.rpartition("@")[0])
            cred = userinfo if ":" in userinfo else _b64(userinfo).decode()
        if cred and host and port:
            out.append((proto, host, int(port), fp(cred)))
    return out


def xray_identities(doc: Any) -> list[Identity]:
    """Native Xray subscription: a JSON list of full configs, first outbound is the proxy."""
    out: list[Identity] = []
    for item in doc if isinstance(doc, list) else []:
        if not isinstance(item, dict):
            continue
        for ob in item.get("outbounds") or []:
            proto = str(ob.get("protocol") or "").lower()
            settings = ob.get("settings") or {}
            if proto in ("vless", "vmess"):
                node = (settings.get("vnext") or [{}])[0]
                user = (node.get("users") or [{}])[0]
                cred = str(user.get("id") or "")
            elif proto in ("trojan", "shadowsocks"):
                node = (settings.get("servers") or [{}])[0]
                cred = str(node.get("password") or "")
                if proto == "shadowsocks":
                    cred = f"{node.get('method')}:{cred}"
            else:
                continue
            host = str(node.get("address") or "").lower()
            port = int(node.get("port") or 0)
            if cred and host and port:
                out.append((proto, host, port, fp(cred)))
            break  # only the first proxy outbound per config
    return out


def singbox_identities(doc: dict) -> list[Identity]:
    out: list[Identity] = []
    for ob in doc.get("outbounds") or []:
        proto = str(ob.get("type") or "").lower()
        if proto not in ("vless", "vmess", "trojan", "shadowsocks"):
            continue
        if proto in ("vless", "vmess"):
            cred = str(ob.get("uuid") or "")
        elif proto == "trojan":
            cred = str(ob.get("password") or "")
        else:
            cred = f"{ob.get('method')}:{ob.get('password')}"
        host = str(ob.get("server") or "").lower()
        port = int(ob.get("server_port") or 0)
        if cred and host and port:
            out.append((proto, host, port, fp(cred)))
    return out


def clash_identities(doc: dict) -> list[Identity]:
    out: list[Identity] = []
    for px in doc.get("proxies") or []:
        ctype = str(px.get("type") or "").lower()
        proto = {"ss": "shadowsocks"}.get(ctype, ctype)
        if proto not in ("vless", "vmess", "trojan", "shadowsocks"):
            continue
        if proto in ("vless", "vmess"):
            cred = str(px.get("uuid") or "")
        elif proto == "trojan":
            cred = str(px.get("password") or "")
        else:
            cred = f"{px.get('cipher')}:{px.get('password')}"
        host = str(px.get("server") or "").lower()
        port = int(px.get("port") or 0)
        if cred and host and port:
            out.append((proto, host, port, fp(cred)))
    return out


def identities_for_format(body: str, config_format: str) -> list[Identity]:
    if config_format == "links":
        return link_identities(body.splitlines())
    if config_format == "links_base64":
        return link_identities(_b64(body.strip()).decode().splitlines())
    if config_format == "xray":
        return xray_identities(json.loads(body))
    if config_format == "sing_box":
        return singbox_identities(json.loads(body))
    if config_format in ("clash", "clash_meta"):
        return clash_identities(yaml.safe_load(body) or {})
    raise ValueError(config_format)


def clash_group_members(doc: dict) -> set[str]:
    """Every proxy name referenced by any proxy-group (to prove entries are selectable)."""
    names: set[str] = set()
    for group in doc.get("proxy-groups") or []:
        names.update(str(p) for p in (group.get("proxies") or []))
    return names


def singbox_group_members(doc: dict) -> set[str]:
    names: set[str] = set()
    for ob in doc.get("outbounds") or []:
        if ob.get("type") in ("selector", "urltest"):
            names.update(str(t) for t in (ob.get("outbounds") or []))
    return names
