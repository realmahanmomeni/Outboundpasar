"""Identify non-production Outbound Center integration endpoints (shared DB test pollution)."""

from __future__ import annotations

_TEST_INTEGRATION_EXACT = frozenset(
    {
        "https://oc.example",
        "http://test-multi-panel",
        "http://test-sec",
        "http://mock",
        "http://timeout",
        "http://err500",
        "http://badjson",
    }
)


def is_test_oc_integration_base_url(base_url: str | None) -> bool:
    url = (base_url or "").strip().lower().rstrip("/")
    if not url:
        return False
    if url in _TEST_INTEGRATION_EXACT:
        return True
    if url.startswith("http://test") or url.startswith("https://test"):
        return True
    if url.startswith("http://t4c_"):
        return True
    if "://mock" in url or url.endswith(".example"):
        return True
    return False
