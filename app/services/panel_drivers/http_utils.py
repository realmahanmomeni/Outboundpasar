"""Small HTTP classification helpers for panel clients."""

from __future__ import annotations

from typing import Any


def looks_like_html_response(raw_text: str, content_type: str | None) -> bool:
    ct = (content_type or "").lower()
    if "html" in ct:
        return True
    lower = (raw_text or "").lstrip().lower()
    return lower.startswith("<!doctype") or lower.startswith("<html")


def is_upstream_unavailable(status_code: int, raw_text: str, content_type: str | None) -> bool:
    if status_code in (502, 503, 504):
        return True
    if status_code == 521:
        return True
    if looks_like_html_response(raw_text, content_type) and status_code >= 500:
        return True
    return False


def looks_like_invalid_credentials(status_code: int, body: Any | None, raw_text: str) -> bool:
    if status_code in (401, 422):
        return True
    hay = (raw_text or "").lower()
    if "invalid" in hay and "credential" in hay:
        return True
    if isinstance(body, dict):
        detail = str(body.get("detail") or body.get("message") or "").lower()
        if "invalid" in detail and ("password" in detail or "username" in detail):
            return True
    return False


def extract_error_detail(body: Any | None, raw_text: str) -> str | None:
    if isinstance(body, dict):
        for key in ("detail", "message", "error", "msg"):
            val = body.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list) and val:
                return str(val[0])
    text = (raw_text or "").strip()
    return text[:240] if text else None


def connection_error_message(
    *,
    method: str,
    url: str,
    status_code: int | None = None,
    raw_text: str = "",
) -> str:
    parts = [f"Panel unreachable ({method} {url})"]
    if status_code is not None:
        parts.append(f"HTTP {status_code}")
    preview = (raw_text or "").strip().replace("\n", " ")
    if preview:
        if len(preview) > 120:
            preview = preview[:117] + "..."
        parts.append(preview)
    return " — ".join(parts)
