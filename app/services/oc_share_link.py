"""Parse proxy share links for OC subscription-backed hosts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app.services.oc_upstream_subscription import _decode_links_payload


def _b64decode_lenient(value: str) -> bytes:
    """Standard or URL-safe base64, padded or not.  ``b64decode(validate=False)`` alone silently
    discards ``-``/``_`` and corrupts URL-safe payloads, so normalise first."""
    cleaned = re.sub(r"\s+", "", value or "").replace("-", "+").replace("_", "/").rstrip("=")
    cleaned += "=" * (-len(cleaned) % 4)
    return base64.b64decode(cleaned, validate=False)


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


# Arabic-script variants that Persian/Arabic keyboards and panels mix freely.
_SCRIPT_FOLD = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه"})
_STRIPPED_FORMAT_CHARS = {"\u200c", "\u200d", "\u200e", "\u200f", "\u0640", "\ufeff"}  # ZWNJ/ZWJ/marks/tatweel/BOM


def _unicode_fold(value: str) -> str:
    """NFKC (folds stylized math alphabets, full-width, ligatures) + casefold + Persian/Arabic unification."""
    text = unicodedata.normalize("NFKC", value or "").casefold().translate(_SCRIPT_FOLD)
    text = "".join(
        ch for ch in text if ch not in _STRIPPED_FORMAT_CHARS and unicodedata.category(ch) not in ("Mn", "Me", "Cf")
    )
    # Persian / Arabic-Indic / other decimal digits -> ASCII digits
    return "".join(str(unicodedata.decimal(ch)) if unicodedata.category(ch) == "Nd" else ch for ch in text)


def _normalize_key(value: str) -> str:
    """Letters and digits of any script, folded; whitespace, punctuation, emoji and flags are ignored.

    Remarks made only of emoji/punctuation fold to ``""``: an empty key never matches anything.
    """
    text = _unicode_fold((value or "").strip())
    return "".join(ch for ch in text if ch.isalnum())


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
    from app.subscription.native_info_display import is_native_informational_display_link

    raw = (link or "").strip()
    if not raw or len(raw) > 8192:
        return False
    if is_native_informational_display_link(raw):
        return False
    lower = raw.lower()
    if lower.startswith("<") or "<html" in lower or "<!doctype" in lower:
        return False
    if not any(lower.startswith(scheme) for scheme in _CONFIGURATION_SCHEMES):
        return False
    if parse_share_link(raw) is not None:
        return True
    return lower.startswith(("hysteria2://", "hy2://", "wireguard://"))


def is_native_informational_display_link(link: str) -> bool:
    """Re-export for subscription import tests and OC pipelines."""
    from app.subscription.native_info_display import is_native_informational_display_link as _is_native_info

    return _is_native_info(link)


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
        # VMess share links may carry a ``#display-remark`` fragment (OC templating uses the same
        # mechanism as vless/trojan). The payload is only the base64 blob before ``#``.
        payload_b64 = raw[len("vmess://") :]
        fragment_remark = ""
        if "#" in payload_b64:
            payload_b64, frag = payload_b64.split("#", 1)
            fragment_remark = unquote(frag)
        payload_b64 = payload_b64.strip()
        data = json.loads(_b64decode_lenient(payload_b64).decode("utf-8", errors="replace"))
        remark = fragment_remark or str(data.get("ps") or data.get("remark") or "")
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
    """Map normalized remark/config keys to parsed links.

    A key shared by links that point at *different endpoints* is ambiguous and is left out of the
    index (callers then fall through to endpoint/structural matching) instead of silently handing the
    first link to whichever config asked first.
    """
    seen: dict[str, ParsedShareLink] = {}
    ambiguous: set[str] = set()
    for link in links:
        parsed = parse_share_link(link)
        if parsed is None:
            continue
        for key in parsed.match_keys():
            prior = seen.get(key)
            if prior is None:
                seen[key] = parsed
            elif share_link_structural_key(prior.raw) != share_link_structural_key(parsed.raw):
                ambiguous.add(key)
    return {k: v for k, v in seen.items() if k not in ambiguous}


def _tokens(value: str) -> set[str]:
    return {t for t in re.findall(r"[^\W_]+", _unicode_fold(value or "")) if len(t) >= 2}


def _remark_hints(remark: str) -> set[str]:
    """ASCII keywords in subscription remarks (handles stylized Unicode labels)."""
    upper = _unicode_fold(remark or "").upper()
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
    norm_id = _normalize_key(source_config_id)
    if norm_id:
        substring_hits = [p for p in pool if norm_id in _normalize_key(p.remark)]
        if len(substring_hits) == 1:
            return substring_hits[0]
    return None


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


def share_link_structural_key(link: str) -> str | None:
    """
    Endpoint identity of a share link that ignores the per-user credential and the remark.

    A panel renders the same endpoint (address, port, transport, TLS/Reality parameters) for every
    user and only the client UUID/password and the ``#remark`` differ.  The discovery (test) user's
    link and a customer's link for the same destination host therefore share this key, which makes
    it a stable identity even when remarks are stylized Unicode or duplicated.

    Returns ``None`` when the scheme carries per-user material that cannot be separated.
    """
    from urllib.parse import urlencode

    base = share_link_config_base((link or "").strip())
    if not base:
        return None
    lower = base.lower()
    try:
        if lower.startswith("vmess://"):
            data = json.loads(_b64decode_lenient(base[len("vmess://") :]).decode("utf-8", "replace"))
            if not isinstance(data, dict):
                return None
            data = {k: v for k, v in data.items() if k not in ("id", "ps", "remark")}
            return "vmess:" + json.dumps(data, sort_keys=True, separators=(",", ":"))
        if lower.startswith(("vless://", "trojan://", "ss://", "hysteria2://", "hy2://")):
            scheme = "hysteria2" if lower.startswith("hy2://") else lower.split("://", 1)[0]
            parsed = urlparse(base)
            host, port = parsed.hostname, parsed.port
            if not host and scheme == "ss":
                # Legacy ss://BASE64(method:password@host:port)
                decoded = base64.urlsafe_b64decode(parsed.netloc + "=" * (-len(parsed.netloc) % 4)).decode(
                    "utf-8", "replace"
                )
                tail = decoded.rsplit("@", 1)[-1]
                host, _, port_text = tail.rpartition(":")
                port = int(port_text) if port_text.isdigit() else None
            if not host or not port:
                return None
            query = sorted(parse_qs(parsed.query, keep_blank_values=True).items())
            flat = [(k, v[0] if v else "") for k, v in query]
            return f"{scheme}://{host.lower()}:{port}?{urlencode(flat)}"
    except Exception:
        return None
    return None


def replace_link_remark(link: str, remark: str) -> str:
    from urllib.parse import quote

    raw = (link or "").strip()
    if raw.lower().startswith("vmess://"):
        # v2rayN and several clients decode the entire post-scheme segment as base64 and do not
        # strip a ``#remark`` suffix. Keep the display name inside the VMess JSON ``ps`` field only.
        base = share_link_config_base(raw)
        payload_b64 = base[len("vmess://") :].strip()
        try:
            data = json.loads(_b64decode_lenient(payload_b64).decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                data["ps"] = remark
                encoded = base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()
                return f"vmess://{encoded}"
        except Exception:
            pass
    base = share_link_config_base(link)
    return f"{base}#{quote(remark)}"
