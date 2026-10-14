"""Non-connectable subscription list entries for scoped native informational Hosts."""

from __future__ import annotations

from urllib.parse import quote

# Reserved identity: must never be treated as an importable upstream proxy (see ``is_importable_configuration_link``).
NATIVE_INFO_DISPLAY_UUID = "00000000-0000-0000-0000-000000000000"
NATIVE_INFO_DISPLAY_HOST = "pasarguard-info.invalid"
NATIVE_INFO_DISPLAY_PORT = 0
NATIVE_INFO_QUERY_FLAG = "pasarguard_info"
NATIVE_INFO_QUERY_VALUE = "1"


def build_native_informational_display_link(formatted_remark: str) -> str | None:
    """
    Build a vless-shaped subscription line whose fragment carries the formatted Host remark.

    The URI uses a fixed non-routable endpoint and is excluded from import/matching pipelines.
    Clients that list subscription lines by fragment can show it as a distinct informational row.
    """
    text = (formatted_remark or "").strip()
    if not text:
        return None
    fragment = quote(text, safe="")
    query = (
        f"encryption=none&security=none&type=tcp&{NATIVE_INFO_QUERY_FLAG}={NATIVE_INFO_QUERY_VALUE}"
    )
    return (
        f"vless://{NATIVE_INFO_DISPLAY_UUID}@{NATIVE_INFO_DISPLAY_HOST}:{NATIVE_INFO_DISPLAY_PORT}"
        f"?{query}#{fragment}"
    )


def is_native_informational_display_link(link: str) -> bool:
    raw = (link or "").strip()
    if not raw.lower().startswith("vless://"):
        return False
    if NATIVE_INFO_DISPLAY_UUID not in raw:
        return False
    if f"{NATIVE_INFO_DISPLAY_HOST}:{NATIVE_INFO_DISPLAY_PORT}" not in raw:
        return False
    if f"{NATIVE_INFO_QUERY_FLAG}={NATIVE_INFO_QUERY_VALUE}" not in raw:
        return False
    return True
