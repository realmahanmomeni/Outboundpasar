"""Valid share URIs remain importable regardless of remark emoji / Unicode."""
from __future__ import annotations

from urllib.parse import quote

from app.services.oc_share_link import (
    decode_subscription_links,
    is_importable_configuration_link,
    parse_share_link,
    subscription_config_links,
)


def _vless(remark: str) -> str:
    frag = quote(remark, safe="")
    return f"vless://11111111-1111-1111-1111-111111111111@1.2.3.4:443?encryption=none&security=tls#{frag}"


def test_remark_variants_do_not_block_vless_import():
    remarks = [
        "Normal TCP",
        "🇳🇱 NL Reality",
        "ایران - تهران",
        "العربية 🇸🇦",
        "user@example.com marketing",
        "x" * 200,
        "SS/TCP 🔥",
    ]
    for remark in remarks:
        link = _vless(remark)
        assert is_importable_configuration_link(link)
        parsed = parse_share_link(link)
        assert parsed is not None
        assert parsed.remark == remark


def test_plaintext_bundle_mixed_metadata_and_unicode_remarks():
    lines = [
        "STATUS: ok",
        _vless("🇮🇷 ایران"),
        "vless://22222222-2222-2222-2222-222222222222@5.6.7.8:8443?encryption=none#"+quote("emoji 🚀", safe=""),
        "not-a-config-line",
    ]
    body = "\n".join(lines)
    decoded = decode_subscription_links(body)
    assert len(decoded) == 2
    dest = subscription_config_links(decoded)
    assert len(dest) == 2


def test_ss_leading_plaintext_subscription_still_decodes():
    body = (
        "ss://Y2hhY2hhMjAtaWV0Zi1wb2x5MTMwNTpQQVNT@host:1080#"+quote("وضعیت", safe="")+"\n"
        + _vless("HTTP-UP") + "\n"
    )
    decoded = decode_subscription_links(body)
    assert len(decoded) >= 2
