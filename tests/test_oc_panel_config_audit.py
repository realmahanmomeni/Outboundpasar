"""OC panel subscription line classification (no network)."""
from __future__ import annotations

from app.services.oc_panel_config_audit import classify_upstream_subscription_body


def test_classify_mixed_subscription_body():
    body = (
        "STATUS: online\n"
        "vless://11111111-1111-1111-1111-111111111111@1.1.1.1:443?encryption=none#"+ "🇮🇷%20Test" + "\n"
        "ftp://invalid.example/resource\n"
        "not a link\n"
    )
    rows, counts = classify_upstream_subscription_body(body)
    assert counts["valid_supported_uri"] == 1
    assert counts["metadata"] == 2
    assert counts["unsupported_scheme"] == 1
    assert len(rows) == 4
