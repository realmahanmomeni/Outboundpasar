"""Valid share URIs remain importable regardless of remark emoji / Unicode."""
from __future__ import annotations

from urllib.parse import quote

from app.services.oc_share_link import (
    decode_subscription_links,
    is_importable_configuration_link,
    is_native_informational_display_link,
    parse_share_link,
    replace_link_remark,
    subscription_config_links,
)
from app.subscription.native_info_display import build_native_informational_display_link


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


def test_native_info_display_link_is_not_importable_but_parsed():
    link = build_native_informational_display_link("Status|{USERNAME}|left")
    assert link is not None
    assert is_native_informational_display_link(link)
    assert not is_importable_configuration_link(link)
    parsed = parse_share_link(link)
    assert parsed is not None
    assert "USERNAME" in parsed.remark


def test_vmess_url_fragment_remark_does_not_break_parse():
    import base64
    import json

    payload = {
        "v": "2",
        "ps": "inner",
        "add": "vm.example",
        "port": 8443,
        "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "aid": 0,
        "net": "ws",
        "type": "none",
        "tls": "tls",
    }
    raw = "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()
    remarked = replace_link_remark(raw, "🇳🇱 Display Name")
    assert "#" not in remarked
    parsed = parse_share_link(remarked)
    assert parsed is not None
    assert parsed.protocol == "vmess"
    assert parsed.address == "vm.example"
    assert parsed.port == 8443
    assert parsed.remark == "🇳🇱 Display Name"
    assert parsed.extra.get("id") == payload["id"]

    # External panels may still append ``#remark``; parsing must tolerate it.
    legacy = f"{remarked}#{quote('legacy', safe='')}"
    legacy_parsed = parse_share_link(legacy)
    assert legacy_parsed is not None
    assert legacy_parsed.remark == "legacy"


def test_ss_leading_plaintext_subscription_still_decodes():
    body = (
        "ss://Y2hhY2hhMjAtaWV0Zi1wb2x5MTMwNTpQQVNT@host:1080#"+quote("وضعیت", safe="")+"\n"
        + _vless("HTTP-UP") + "\n"
    )
    decoded = decode_subscription_links(body)
    assert len(decoded) >= 2
