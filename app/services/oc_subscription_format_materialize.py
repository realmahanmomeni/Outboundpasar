"""Build structured subscription entries (Xray JSON / sing-box / Clash) from authoritative OC links.

OC users receive their configs as share links collected from the user's own seller-panel
subscription, filtered by the user's group selection.  Structured clients (Happ, Hiddify,
sing-box, Clash...) cannot consume those links directly, so each link is converted back into the
internal ``SubscriptionInboundData`` model and handed to the *same* serializers used for native
hosts (``XrayConfiguration``, ``SingBoxConfiguration``, ``Clash*Configuration``).

That shared path is deliberate: entries are emitted before the template is rendered, so they take
part in ``proxy-groups`` / ``selector`` / ``urltest`` exactly like native entries, and every
transport/TLS/REALITY field is serialized with the schema that is already validated for native
output.  There is no OC-specific JSON/YAML dialect.

The reverse mapping in :func:`parsed_link_to_inbound` is the inverse of
``app.subscription.links.StandardLinks`` plus the common v2rayN/SIP002 spellings seen from sellers.
Links that cannot be represented faithfully are *excluded and counted*, never reinterpreted.
"""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import ValidationError

from app.models.subscription import (
    GRPCTransportConfig,
    KCPTransportConfig,
    QUICTransportConfig,
    SubscriptionInboundData,
    TCPTransportConfig,
    TLSConfig,
    WebSocketTransportConfig,
    XHTTPTransportConfig,
)
from app.services.oc_share_link import ParsedShareLink, parse_share_link, share_link_config_base
from app.utils.logger import get_logger

logger = get_logger("oc-format-materialize")

# Protocols that cannot be converted to structured client formats from a share link alone
# (hysteria2 needs finalmask/QUIC params, wireguard needs a private key + interface address).
STRUCTURED_UNSUPPORTED_PROTOCOLS = frozenset({"hysteria2", "wireguard"})

STRUCTURED_FORMATS = frozenset({"xray", "sing_box", "clash", "clash_meta"})

_SUPPORTED_PROTOCOLS = ("vless", "vmess", "trojan", "shadowsocks")
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MaterializeResult:
    payload: str | None
    input_link_count: int
    represented_count: int
    skipped_unsupported: int
    # Links the target serializer deliberately does not emit (e.g. VLESS in classic Clash).
    skipped_by_format: int = 0
    # Links that could not be parsed/represented (malformed fields).
    skipped_invalid: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# link -> SubscriptionInboundData
# ---------------------------------------------------------------------------


def _b64decode_lenient(value: str) -> bytes:
    """Standard or URL-safe base64, with or without padding, tolerant of whitespace."""
    cleaned = re.sub(r"\s+", "", value or "").replace("-", "+").replace("_", "/")
    cleaned = cleaned.rstrip("=")
    cleaned += "=" * (-len(cleaned) % 4)
    return base64.b64decode(cleaned, validate=False)


def _userinfo(raw_base: str) -> str:
    """Raw (percent-decoded) userinfo, keeping any ``:`` that urllib would split on."""
    netloc = urlsplit(raw_base).netloc
    if "@" not in netloc:
        return ""
    return unquote(netloc.rpartition("@")[0])


def _flag(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _as_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalize_network(network: str | None) -> str:
    net = (network or "tcp").strip().lower()
    if net == "http":
        return "h2"
    if net == "splithttp":
        return "xhttp"
    return net or "tcp"


# camelCase keys written by StandardLinks._transport_xhttp ``extra`` -> XHTTPTransportConfig fields
_XHTTP_EXTRA_FIELDS = {
    "scMaxEachPostBytes": "sc_max_each_post_bytes",
    "scMinPostsIntervalMs": "sc_min_posts_interval_ms",
    "xPaddingBytes": "x_padding_bytes",
    "xPaddingObfsMode": "x_padding_obfs_mode",
    "xPaddingKey": "x_padding_key",
    "xPaddingHeader": "x_padding_header",
    "xPaddingPlacement": "x_padding_placement",
    "xPaddingMethod": "x_padding_method",
    "uplinkHTTPMethod": "uplink_http_method",
    "sessionIDPlacement": "session_placement",
    "sessionIDKey": "session_key",
    "sessionIDTable": "session_id_table",
    "sessionIDLength": "session_id_length",
    "seqPlacement": "seq_placement",
    "seqKey": "seq_key",
    "uplinkDataPlacement": "uplink_data_placement",
    "uplinkDataKey": "uplink_data_key",
    "uplinkChunkSize": "uplink_chunk_size",
    "noGRPCHeader": "no_grpc_header",
    "xmux": "xmux",
    "headers": "http_headers",
    "downloadSettings": "download_settings",
}


def _link_params(parsed: ParsedShareLink) -> dict[str, str]:
    """Flat, case-preserving query/JSON parameters for a parsed link."""
    return {k: ("" if v is None else str(v)) for k, v in (parsed.extra or {}).items()}


def _transport_config(
    protocol: str, network: str, params: dict[str, str]
) -> (
    GRPCTransportConfig
    | WebSocketTransportConfig
    | XHTTPTransportConfig
    | KCPTransportConfig
    | QUICTransportConfig
    | TCPTransportConfig
):
    path = unquote(params.get("path") or "")
    host = (params.get("host") or "").strip()
    header_type = (params.get("headerType") or "").strip().lower()
    if network == "ws":
        cfg = WebSocketTransportConfig(path=path, host=host)
        hb = _as_int(params.get("heartbeatPeriod"))
        if hb:
            cfg.heartbeat_period = hb
        return cfg
    if network == "httpupgrade":
        return WebSocketTransportConfig(path=path, host=host)
    if network in ("grpc", "gun"):
        # StandardLinks writes serviceName for vless/trojan and (path, type=gun|multi) for vmess.
        service = unquote(params.get("serviceName") or params.get("path") or "")
        mode = (params.get("mode") or params.get("type") or "").lower()
        authority = (params.get("authority") or params.get("host") or "").strip()
        return GRPCTransportConfig(path=service, host=authority, multi_mode=(mode == "multi"))
    if network == "xhttp":
        mode = params.get("mode") or (params.get("type") if protocol == "vmess" else "") or "auto"
        values: dict[str, Any] = {"path": path, "host": host, "mode": mode}
        extra_raw = params.get("extra") or ""
        if extra_raw:
            extra = json.loads(extra_raw)
            if isinstance(extra, dict):
                for key, value in extra.items():
                    target = _XHTTP_EXTRA_FIELDS.get(key)
                    if target:
                        values[target] = value
        return XHTTPTransportConfig(**values)
    if network == "kcp":
        return KCPTransportConfig(
            path=path,
            host=host,
            mtu=_as_int(params.get("mtu")),
            tti=_as_int(params.get("tti")),
        )
    if network == "quic":
        return QUICTransportConfig(
            path=unquote(params.get("key") or path),
            host=(params.get("quicSecurity") or host),
            header_type=header_type or "none",
        )
    # tcp / raw / h2 (xray "http"): header type "http" asks for HTTP obfuscation on tcp.
    return TCPTransportConfig(
        path=path,
        host=host,
        header_type=header_type if header_type in ("http", "none") else "none",
    )


def _tls_config(parsed: ParsedShareLink, params: dict[str, str]) -> TLSConfig:
    raw_security = params.get("security")
    if raw_security is None:
        raw_security = params.get("tls")
    if raw_security is None and parsed.protocol == "trojan":
        # Trojan has no cleartext mode in practice; links without any security key are TLS by
        # convention in v2rayN/v2rayNG.  An explicit ``security=none`` is still honoured.
        raw_security = "tls"
    security = (raw_security or "").strip().lower()
    tls = security if security in ("tls", "reality") else None
    if tls is None:
        return TLSConfig(tls=None)
    sni = (params.get("sni") or params.get("peer") or "").strip()
    alpn = [item.strip() for item in (params.get("alpn") or "").split(",") if item.strip()]
    verify_names = [item.strip() for item in (params.get("vcn") or "").split(",") if item.strip()]
    return TLSConfig(
        tls=tls,
        sni=sni,
        fingerprint=(params.get("fp") or "").strip(),
        allowinsecure=_flag(params.get("allowInsecure") or params.get("allow_insecure") or params.get("insecure")),
        alpn_list=alpn,
        ech_config_list=(params.get("ech") or None),
        ech_query_strategy=(params.get("echForceQuery") or None),
        pinned_peer_cert_sha256=(params.get("pcs") or params.get("pinSHA256") or None),
        verify_peer_cert_by_name=verify_names or [],
        reality_public_key=(params.get("pbk") or "").strip(),
        reality_short_id=(params.get("sid") or "").strip(),
        reality_spx=(params.get("spx") or "").strip(),
        mldsa65_verify=(params.get("pqv") or None),
    )


def _shadowsocks_credentials(raw_base: str) -> tuple[str, str, str, int] | None:
    """(method, password, host, port) for SIP002 (``ss://b64(m:p)@h:p`` or ``ss://m:p@h:p``) and legacy."""
    split = urlsplit(raw_base)
    netloc = split.netloc
    if "@" in netloc:
        userinfo_raw, _, hostport = netloc.rpartition("@")
        host = split.hostname or ""
        port = split.port
        userinfo = unquote(userinfo_raw)
        if ":" not in userinfo:
            userinfo = _b64decode_lenient(userinfo).decode("utf-8", errors="strict")
        method, sep, password = userinfo.partition(":")
        if not sep or not method or not host or not port:
            return None
        return method, password, host, int(port)
    # Legacy: ss://BASE64(method:password@host:port)
    decoded = _b64decode_lenient(netloc).decode("utf-8", errors="strict")
    creds, sep, tail = decoded.rpartition("@")
    if not sep:
        return None
    method, sep2, password = creds.partition(":")
    host, sep3, port_text = tail.rpartition(":")
    port = _as_int(port_text)
    if not sep2 or not sep3 or not method or not host or not port:
        return None
    return method, password, host.strip("[]"), port


def parsed_link_to_inbound(
    parsed: ParsedShareLink,
) -> tuple[SubscriptionInboundData, dict[str, Any], str] | None:
    """
    Convert a parsed share link to ``(inbound, user_settings, address)`` for the shared serializers.

    Returns ``None`` when the link is not representable.  Raises nothing: malformed input is
    reported as ``None`` so the caller can count and log it explicitly.
    """
    try:
        return _parsed_link_to_inbound(parsed)
    except (ValueError, TypeError, KeyError, ValidationError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _parsed_link_to_inbound(
    parsed: ParsedShareLink,
) -> tuple[SubscriptionInboundData, dict[str, Any], str] | None:
    protocol = parsed.protocol
    if protocol not in _SUPPORTED_PROTOCOLS:
        return None
    raw_base = share_link_config_base(parsed.raw)
    params = _link_params(parsed)

    address = (parsed.address or "").strip()
    port = parsed.port
    settings: dict[str, Any]
    inbound_flow = ""
    encryption = "none"

    if protocol == "vmess":
        uid = str(params.get("id") or "").strip()
        if not uid:
            return None
        settings = {"id": uid}
        network = _normalize_network(params.get("net"))
        if network == "tcp" and (params.get("type") or "").strip().lower() == "http":
            params["headerType"] = "http"
        if network in ("grpc", "gun", "xhttp"):
            params["mode"] = params.get("type", "")
        elif network not in ("tcp",):
            params["headerType"] = params.get("type", "")
    elif protocol == "vless":
        uid = _userinfo(raw_base).strip()
        if not uid:
            return None
        settings = {"id": uid}
        network = _normalize_network(parsed.network or params.get("type"))
        inbound_flow = (params.get("flow") or "").strip()
        encryption = (params.get("encryption") or "none").strip() or "none"
    elif protocol == "trojan":
        password = _userinfo(raw_base)
        if not password:
            return None
        settings = {"password": password}
        network = _normalize_network(parsed.network or params.get("type"))
    else:  # shadowsocks
        if (params.get("plugin") or "").strip():
            # Plugin-based shadowsocks cannot be expressed by the structured serializers.
            return None
        creds = _shadowsocks_credentials(raw_base)
        if creds is None:
            return None
        method, password, address, ss_port = creds
        port = ss_port
        settings = {"method": method, "password": password}
        network = "tcp"

    if not address or not port:
        return None

    if protocol == "shadowsocks":
        tls_config = TLSConfig(tls=None)
        transport = TCPTransportConfig()
    else:
        tls_config = _tls_config(parsed, params)
        transport = _transport_config(protocol, network, params)

    inbound = SubscriptionInboundData(
        remark=parsed.remark or "",
        inbound_tag="oc_materialized",
        protocol=protocol,
        address=address,
        port=int(port),
        network=network,
        tls_config=tls_config,
        transport_config=transport,
        encryption=encryption,
        inbound_flow=inbound_flow,
    )
    return inbound, settings, address


# ---------------------------------------------------------------------------
# conf helpers
# ---------------------------------------------------------------------------

_SINGBOX_PROXY_TYPES = frozenset({"vmess", "vless", "trojan", "shadowsocks", "hysteria2", "tuic", "http", "ssh"})


def conf_proxy_count(conf: Any) -> int:
    """Number of connectable entries already added to a subscription configuration object."""
    from app.subscription import (
        ClashConfiguration,
        SingBoxConfiguration,
        StandardLinks,
        XrayConfiguration,
    )

    if isinstance(conf, StandardLinks):
        return len(conf.links)
    if isinstance(conf, XrayConfiguration):
        return len(conf.config)
    if isinstance(conf, SingBoxConfiguration):
        outbounds = [o for o in conf.config.get("outbounds", []) if o.get("type") in _SINGBOX_PROXY_TYPES]
        return len(outbounds) + len(conf.config.get("endpoints", []))
    if isinstance(conf, ClashConfiguration):
        return len(conf.data.get("proxies", []))
    return 0


def _prepare_links(links: list[str]) -> list[tuple[str, ParsedShareLink | None]]:
    """Strip, parse once and drop only *exact* duplicates (same config base and same remark).

    Two links with the same credential/endpoint but different OC display names are distinct
    selected hosts and must both survive, matching what the plain-links output shows.
    Order is preserved.
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, ParsedShareLink | None]] = []
    for link in links:
        raw = (link or "").strip().lstrip("\ufeff\u200b\u200c\u200d\u200e\u200f").strip()
        if not raw:
            continue
        parsed = parse_share_link(raw)
        key = (share_link_config_base(raw), parsed.remark if parsed else raw.partition("#")[2])
        if key in seen:
            continue
        seen.add(key)
        out.append((raw, parsed))
    return out


def _dedupe_links(links: list[str]) -> list[str]:
    return [raw for raw, _ in _prepare_links(links)]


def add_authoritative_oc_links_to_conf(
    conf: Any,
    allowed_links: list[str],
    *,
    xray_template_content: str | None = None,
) -> MaterializeResult:
    """
    Add every representable authorized OC link to ``conf`` via its native ``add`` method.

    ``conf`` must be a (not yet rendered) Xray/sing-box/Clash/Clash-Meta configuration.  Entries are
    appended in input order, so output order equals the (deterministic) order of ``allowed_links``.
    """
    from app.subscription import XrayConfiguration

    prepared = _prepare_links(allowed_links)
    skipped_unsupported = 0
    skipped_by_format = 0
    skipped_invalid = 0
    reasons: Counter[str] = Counter()
    before = conf_proxy_count(conf)

    for _link, parsed in prepared:
        if parsed is None:
            skipped_invalid += 1
            reasons["unparseable"] += 1
            continue
        if parsed.protocol in STRUCTURED_UNSUPPORTED_PROTOCOLS:
            skipped_unsupported += 1
            reasons[f"unsupported_protocol:{parsed.protocol}"] += 1
            continue
        converted = parsed_link_to_inbound(parsed)
        if converted is None:
            skipped_invalid += 1
            reasons[f"unrepresentable:{parsed.protocol}"] += 1
            continue
        inbound, settings, address = converted
        count_before = conf_proxy_count(conf)
        if isinstance(conf, XrayConfiguration):
            conf.add(
                remark=parsed.remark or "",
                address=address,
                inbound=inbound,
                settings=settings,
                template_content=xray_template_content,
            )
        else:
            conf.add(remark=parsed.remark or "", address=address, inbound=inbound, settings=settings)
        if conf_proxy_count(conf) == count_before:
            skipped_by_format += 1
            reasons[f"not_supported_by_format:{parsed.protocol}/{inbound.network}"] += 1

    return MaterializeResult(
        payload=None,
        input_link_count=len(prepared),
        represented_count=conf_proxy_count(conf) - before,
        skipped_unsupported=skipped_unsupported,
        skipped_by_format=skipped_by_format,
        skipped_invalid=skipped_invalid,
        skip_reasons=dict(reasons),
    )


# ---------------------------------------------------------------------------
# standalone materialization (diagnostics / tests)
# ---------------------------------------------------------------------------


def _default_templates() -> dict[str, str]:
    """Default client templates (only used when no real templates are supplied)."""
    from importlib import import_module

    module = import_module("app.db.migrations.versions.e8c6a4f1d2b7_add_client_templates_table")
    return {
        "CLASH_SUBSCRIPTION_TEMPLATE": module.DEFAULT_CLASH_SUBSCRIPTION_TEMPLATE,
        "XRAY_SUBSCRIPTION_TEMPLATE": module.DEFAULT_XRAY_SUBSCRIPTION_TEMPLATE,
        "SINGBOX_SUBSCRIPTION_TEMPLATE": module.DEFAULT_SINGBOX_SUBSCRIPTION_TEMPLATE,
        "USER_AGENT_TEMPLATE": module.DEFAULT_USER_AGENT_TEMPLATE,
        "GRPC_USER_AGENT_TEMPLATE": module.DEFAULT_GRPC_USER_AGENT_TEMPLATE,
    }


def materialize_authoritative_oc_payload(
    allowed_links: list[str],
    config_format: str,
    client_templates: dict[str, str] | None = None,
) -> str | None:
    return materialize_authoritative_oc_payload_detailed(allowed_links, config_format, client_templates).payload


def materialize_authoritative_oc_payload_detailed(
    allowed_links: list[str],
    config_format: str,
    client_templates: dict[str, str] | None = None,
) -> MaterializeResult:
    """
    Render a complete standalone subscription body for ``config_format`` from authorized links.

    Production requests do not use this: they add the same entries to the live configuration object
    inside ``process_inbounds_and_tags``.  This wrapper exists for diagnostics and tests and goes
    through the identical serializers, so its output is the production output for those links.
    """
    from app.subscription.share import _build_subscription_config

    links = _dedupe_links(allowed_links)
    if not links:
        return MaterializeResult(None, 0, 0, 0)
    if config_format not in STRUCTURED_FORMATS:
        if config_format == "outline":
            return MaterializeResult("{}", len(links), 0, 0)
        return MaterializeResult(None, len(links), 0, 0)

    conf = _build_subscription_config(config_format, client_templates or _default_templates())
    if conf is None:
        return MaterializeResult(None, len(links), 0, 0)
    result = add_authoritative_oc_links_to_conf(conf, links)
    payload = conf.render() if result.represented_count else None
    return MaterializeResult(
        payload=payload,
        input_link_count=result.input_link_count,
        represented_count=result.represented_count,
        skipped_unsupported=result.skipped_unsupported,
        skipped_by_format=result.skipped_by_format,
        skipped_invalid=result.skipped_invalid,
        skip_reasons=result.skip_reasons,
    )
