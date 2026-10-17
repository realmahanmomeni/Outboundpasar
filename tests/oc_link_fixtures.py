"""Sanitized synthetic share-link fixtures (no real hosts, tokens or credentials).

Credentials are derived from a label with UUIDv5 / fixed strings so tests are deterministic and
the same label always yields the same fingerprint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from urllib.parse import quote

_NS = uuid.UUID("00000000-0000-4000-8000-0000000000aa")


def reality_public_key(label: str = "default") -> str:
    """Well-formed (43-char base64url) synthetic x25519 public key - parsers reject malformed ones."""
    return base64.urlsafe_b64encode(hashlib.sha256(f"pbk:{label}".encode()).digest()).decode().rstrip("=")


def cred(label: str) -> str:
    """Deterministic synthetic UUID credential."""
    return str(uuid.uuid5(_NS, label))


def vless(
    label: str,
    host: str,
    port: int = 443,
    /,
    *,
    remark: str | None = None,
    network: str = "tcp",
    security: str = "tls",
    **params: str,
) -> str:
    query: dict[str, str] = {"encryption": "none", "security": security, "type": network}
    if security in ("tls", "reality"):
        query.setdefault("sni", host)
        query.setdefault("fp", "chrome")
    if security == "reality":
        query.setdefault("pbk", reality_public_key(label))
        query.setdefault("sid", "ab12cd34")
        query.setdefault("spx", "/")
    if network == "ws":
        query.setdefault("path", "/ws")
        query.setdefault("host", f"cdn.{host}")
    if network == "httpupgrade":
        query.setdefault("path", "/hu")
        query.setdefault("host", host)
    if network == "grpc":
        query.setdefault("serviceName", "svc")
        query.setdefault("mode", "gun")
    if network == "xhttp":
        query.setdefault("path", "/xh")
        query.setdefault("host", host)
        query.setdefault("mode", "auto")
    if network == "h2":
        query.setdefault("path", "/h2")
        query.setdefault("host", host)
    query.update(params)
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in query.items())
    return f"vless://{cred(label)}@{host}:{port}?{qs}#{quote(remark if remark is not None else label)}"


def vmess(
    label: str,
    host: str,
    port: int = 443,
    /,
    *,
    remark: str | None = None,
    network: str = "ws",
    tls: str = "tls",
    urlsafe: bool = False,
    pad: bool = True,
    **extra,
) -> str:
    obj = {
        "v": "2",
        "ps": remark if remark is not None else label,
        "add": host,
        "port": port,
        "id": cred(label),
        "aid": 0,
        "scy": "auto",
        "net": network,
        "type": "none",
        "host": f"cdn.{host}" if network in ("ws", "httpupgrade") else "",
        "path": "/vm" if network in ("ws", "httpupgrade") else "",
        "tls": tls,
        "sni": host if tls == "tls" else "",
    }
    if network == "grpc":
        obj["path"] = "svc"
        obj["type"] = "gun"
    obj.update(extra)
    raw = json.dumps(obj, ensure_ascii=False).encode()
    enc = base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)
    text = enc.decode()
    if not pad:
        text = text.rstrip("=")
    return "vmess://" + text


def trojan(
    label: str,
    host: str,
    port: int = 443,
    /,
    *,
    remark: str | None = None,
    network: str = "tcp",
    security: str = "tls",
    **params: str,
) -> str:
    query: dict[str, str] = {"security": security, "type": network}
    if security == "tls":
        query.setdefault("sni", host)
    if network == "ws":
        query.setdefault("path", "/tws")
        query.setdefault("host", f"cdn.{host}")
    if network == "grpc":
        query.setdefault("serviceName", "tsvc")
        query.setdefault("mode", "gun")
    query.update(params)
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in query.items())
    return f"trojan://{quote(cred(label), safe=':')}@{host}:{port}?{qs}#{quote(remark if remark is not None else label)}"


def shadowsocks(
    label: str,
    host: str,
    port: int = 8388,
    /,
    *,
    remark: str | None = None,
    method: str = "chacha20-ietf-poly1305",
    plain: bool = False,
) -> str:
    password = cred(label)
    if plain:
        userinfo = f"{method}:{password}"
    else:
        userinfo = base64.b64encode(f"{method}:{password}".encode()).decode()
    return f"ss://{userinfo}@{host}:{port}#{quote(remark if remark is not None else label)}"


def hysteria2(label: str, host: str, port: int = 443, /, *, remark: str | None = None) -> str:
    return f"hysteria2://{cred(label)}@{host}:{port}/?sni={host}#{quote(remark if remark is not None else label)}"


def wireguard(label: str, host: str, port: int = 51820, /, *, remark: str | None = None) -> str:
    return f"wireguard://{quote(cred(label), safe='')}@{host}:{port}/?publickey=SYNTH#{quote(remark if remark is not None else label)}"
