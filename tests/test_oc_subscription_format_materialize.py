"""Unit tests: authoritative OC share links -> native structured subscription formats.

Structured output is produced by the *native* PasarGuard serializers (Xray / sing-box / Clash), so
each client receives the schema its parser expects.  Identity (protocol, host, port, credential
fingerprint) of every represented link is asserted; raw credentials never appear in failures.

Real-parser validation (sing-box / Xray / mihomo) runs only when the binary is configured through
``SINGBOX_BIN`` / ``XRAY_BIN`` / ``MIHOMO_BIN`` and is reported as *skipped* otherwise.
"""

from __future__ import annotations

import base64
import json

import pytest
import yaml

from app.services.oc_subscription_format_materialize import (
    materialize_authoritative_oc_payload,
    materialize_authoritative_oc_payload_detailed,
)
from tests import oc_client_validators as validators
from tests import oc_link_fixtures as F
from tests.oc_format_identity import (
    clash_group_members,
    identities_for_format,
    link_identities,
    singbox_group_members,
)

ALL = frozenset({"xray", "sing_box", "clash_meta", "clash"})
NO_CLASSIC_CLASH = frozenset({"xray", "sing_box", "clash_meta"})
NO_SINGBOX = frozenset({"xray", "clash_meta"})

# (id, link, formats that can represent it)
MATRIX = [
    ("vless-tcp-tls", F.vless("a", "a.example"), NO_CLASSIC_CLASH),
    ("vless-tcp-plain", F.vless("b", "b.example", security="none"), NO_CLASSIC_CLASH),
    ("vless-ws-tls", F.vless("c", "c.example", network="ws"), NO_CLASSIC_CLASH),
    ("vless-grpc-tls", F.vless("d", "d.example", network="grpc"), NO_CLASSIC_CLASH),
    ("vless-httpupgrade-tls", F.vless("e", "e.example", network="httpupgrade"), NO_CLASSIC_CLASH),
    (
        "vless-reality-vision",
        F.vless("f", "f.example", security="reality", flow="xtls-rprx-vision"),
        NO_CLASSIC_CLASH,
    ),
    ("vless-xhttp-tls", F.vless("g", "g.example", network="xhttp"), NO_SINGBOX),
    ("vless-h2-tls", F.vless("h", "h.example", network="h2"), NO_CLASSIC_CLASH),
    ("vmess-ws-tls", F.vmess("i", "i.example"), ALL),
    ("vmess-tcp-plain", F.vmess("j", "j.example", network="tcp", tls=""), ALL),
    ("vmess-grpc-tls", F.vmess("k", "k.example", network="grpc"), ALL),
    ("trojan-tcp-tls", F.trojan("l", "l.example"), ALL),
    ("trojan-ws-tls", F.trojan("m", "m.example", network="ws"), ALL),
    ("trojan-grpc-tls", F.trojan("n", "n.example", network="grpc"), ALL),
    ("ss-base64-userinfo", F.shadowsocks("o", "o.example"), ALL),
    ("ss-plain-userinfo", F.shadowsocks("p", "p.example", plain=True), ALL),
]
IDS = [case[0] for case in MATRIX]

# Cells where the generated entry is valid for PasarGuard's serializer but a *current* real parser
# rejects the transport itself.  Documented limitation, not an OC defect: Xray-core 26.x removed
# the legacy ``http`` (h2) transport that the native Xray serializer still emits.
XRAY_PARSER_REJECTS = {"vless-h2-tls"}


# --------------------------------------------------------------------------------------------
# identity parity per (protocol/transport) x format
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", sorted(ALL))
@pytest.mark.parametrize(("case_id", "link", "supported"), MATRIX, ids=IDS)
def test_identity_matrix(case_id, link, supported, fmt):
    result = materialize_authoritative_oc_payload_detailed([link], fmt)
    if fmt not in supported:
        assert result.payload is None, f"{case_id} must not be partially rendered for {fmt}"
        assert result.represented_count == 0
        assert result.skipped_by_format == 1
        assert result.skip_reasons, "skip must carry a reason, never be silent"
        return
    assert result.represented_count == 1, result.skip_reasons
    assert identities_for_format(result.payload, fmt) == link_identities([link])


@pytest.mark.parametrize("fmt", sorted(ALL))
def test_mixed_batch_keeps_order_and_accounts_for_every_link(fmt):
    links = [case[1] for case in MATRIX]
    result = materialize_authoritative_oc_payload_detailed(links, fmt)
    expected = [i for case in MATRIX if fmt in case[2] for i in link_identities([case[1]])]
    assert identities_for_format(result.payload, fmt) == expected
    assert result.represented_count == len(expected)
    assert (
        result.represented_count + result.skipped_unsupported + result.skipped_by_format + result.skipped_invalid
        == result.input_link_count
    ), "every input link is either represented or explicitly accounted for"


@pytest.mark.parametrize("fmt", ["xray", "sing_box", "clash_meta"])
def test_output_is_deterministic(fmt):
    links = [case[1] for case in MATRIX]
    first = materialize_authoritative_oc_payload(links, fmt)
    for _ in range(3):
        assert materialize_authoritative_oc_payload(links, fmt) == first


# --------------------------------------------------------------------------------------------
# native schema (Happ / Xray and Hiddify / sing-box regressions)
# --------------------------------------------------------------------------------------------


def test_xray_entries_are_native_full_configs_not_flat_objects():
    """Happ consumes the native schema: a JSON list of full Xray configs, never flat link dicts."""
    links = [F.vless("a", "a.example"), F.vmess("b", "b.example")]
    data = json.loads(materialize_authoritative_oc_payload(links, "xray"))
    assert isinstance(data, list) and len(data) == 2
    for entry in data:
        assert isinstance(entry.get("outbounds"), list) and entry["outbounds"]
        assert entry.get("remarks")
        for flat_key in ("id", "add", "port", "protocol", "net"):
            assert flat_key not in entry, f"flat legacy key {flat_key!r} leaked into top level"
        assert entry["outbounds"][0]["protocol"] in ("vless", "vmess")


def test_singbox_ws_has_no_unknown_transport_host_field():
    """Regression: `outbounds[N].transport.host: json: unknown field "host"` (Hiddify import)."""
    ws = F.vless("c", "c.example", network="ws", host="cdn.c.example", path="/ws")
    doc = json.loads(materialize_authoritative_oc_payload([ws], "sing_box"))
    (outbound,) = [o for o in doc["outbounds"] if o.get("type") == "vless"]
    transport = outbound["transport"]
    assert transport["type"] == "ws"
    assert "host" not in transport
    header_host = transport["headers"]["host"]  # sing-box Listable[string]: str or [str]
    assert (header_host if isinstance(header_host, list) else [header_host]) == ["cdn.c.example"]
    assert transport["path"] == "/ws"


def test_singbox_transport_schema_per_transport():
    docs = {
        name: json.loads(materialize_authoritative_oc_payload([link], "sing_box"))
        for name, link in {
            "httpupgrade": F.vless("e", "e.example", network="httpupgrade", host="up.e.example"),
            "grpc": F.vless("d", "d.example", network="grpc", serviceName="svcname"),
        }.items()
    }
    (httpupgrade,) = [o for o in docs["httpupgrade"]["outbounds"] if o.get("type") == "vless"]
    assert httpupgrade["transport"]["type"] == "httpupgrade"
    assert httpupgrade["transport"]["host"] == "up.e.example"  # httpupgrade really has a string host
    (grpc,) = [o for o in docs["grpc"]["outbounds"] if o.get("type") == "vless"]
    assert grpc["transport"]["type"] == "grpc"
    assert grpc["transport"]["service_name"] == "svcname"


def test_singbox_reality_and_tls_metadata_preserved():
    link = F.vless("f", "f.example", security="reality", flow="xtls-rprx-vision", sni="front.example", fp="firefox")
    doc = json.loads(materialize_authoritative_oc_payload([link], "sing_box"))
    (outbound,) = [o for o in doc["outbounds"] if o.get("type") == "vless"]
    assert outbound["flow"] == "xtls-rprx-vision"
    tls = outbound["tls"]
    assert tls["enabled"] is True
    assert tls["server_name"] == "front.example"
    assert tls["reality"]["enabled"] is True
    assert tls["reality"]["public_key"] == F.reality_public_key("f")
    assert tls["reality"]["short_id"] == "ab12cd34"
    assert tls["utls"]["fingerprint"] == "firefox"


def test_xray_reality_and_ws_stream_settings_preserved():
    reality = F.vless("f", "f.example", security="reality", flow="xtls-rprx-vision")
    ws = F.vless("c", "c.example", network="ws", host="cdn.c.example", path="/ws")
    data = json.loads(materialize_authoritative_oc_payload([reality, ws], "xray"))
    stream_r = data[0]["outbounds"][0]["streamSettings"]
    assert stream_r["security"] == "reality"
    assert stream_r["realitySettings"]["publicKey"] == F.reality_public_key("f")
    assert stream_r["realitySettings"]["shortId"] == "ab12cd34"
    stream_w = data[1]["outbounds"][0]["streamSettings"]
    assert stream_w["network"] == "ws"
    assert stream_w["wsSettings"]["path"] == "/ws"
    assert stream_w["wsSettings"]["host"] == "cdn.c.example"


def test_classic_clash_excludes_vless_like_native():
    """Classic Clash has no VLESS handler: entries are skipped *explicitly*, never mangled."""
    result = materialize_authoritative_oc_payload_detailed([F.vless("a", "a.example")], "clash")
    assert result.payload is None
    assert result.skip_reasons == {"not_supported_by_format:vless/tcp": 1}


def test_clash_and_singbox_groups_select_every_represented_entry():
    links = [F.vless("a", "a.example"), F.vmess("b", "b.example"), F.trojan("c", "c.example")]
    clash = yaml.safe_load(materialize_authoritative_oc_payload(links, "clash_meta"))
    names = {p["name"] for p in clash["proxies"]}
    assert len(names) == 3
    assert names <= clash_group_members(clash), "OC proxies must be selectable through a proxy-group"

    sing = json.loads(materialize_authoritative_oc_payload(links, "sing_box"))
    tags = {o["tag"] for o in sing["outbounds"] if o.get("type") in ("vless", "vmess", "trojan")}
    assert len(tags) == 3
    assert tags <= singbox_group_members(sing), "OC outbounds must be members of selector/urltest"


# --------------------------------------------------------------------------------------------
# encodings seen from real sellers (v2rayNG / v2rayN style link bodies)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("urlsafe", [False, True])
@pytest.mark.parametrize("pad", [True, False])
def test_vmess_base64_variants_decode_to_same_identity(urlsafe, pad):
    # Unicode remark forces non-ASCII bytes through the base64 payload, which is where url-safe
    # alphabets differ from standard ones.
    link = F.vmess("v", "v.example", remark="Zürich ★ 東京 ??>>", urlsafe=urlsafe, pad=pad)
    expected = link_identities([link])
    assert expected, "fixture must be parseable"
    for fmt in ("xray", "sing_box", "clash_meta"):
        payload = materialize_authoritative_oc_payload([link], fmt)
        assert identities_for_format(payload, fmt) == expected


@pytest.mark.parametrize("decorate", [lambda s: s + "\r", lambda s: "\ufeff" + s, lambda s: "  " + s + "  "])
def test_link_whitespace_bom_and_crlf_are_tolerated(decorate):
    link = F.vless("a", "a.example")
    payload = materialize_authoritative_oc_payload([decorate(link)], "xray")
    assert identities_for_format(payload, "xray") == link_identities([link])


def test_unicode_and_percent_encoded_remarks_survive_in_every_format():
    remark = "🇩🇪 Berlin | 100% پیام"
    links = [F.vless("a", "a.example", remark=remark), F.trojan("b", "b.example", remark=remark)]
    xray = json.loads(materialize_authoritative_oc_payload(links, "xray"))
    assert [e["remarks"] for e in xray] == [remark, remark]


# --------------------------------------------------------------------------------------------
# identity safety: dedupe must never merge distinct endpoints / credentials
# --------------------------------------------------------------------------------------------


def test_exact_duplicate_links_collapse_to_one_entry():
    link = F.vless("a", "a.example")
    payload = materialize_authoritative_oc_payload([link, link], "xray")
    assert len(json.loads(payload)) == 1


def test_same_endpoint_different_remarks_are_both_kept():
    """Distinct remarks are distinct entries a user can pick; silently dropping one loses configs."""
    a = F.vless("same", "h.example", remark="A")
    b = F.vless("same", "h.example", remark="B")
    xray = json.loads(materialize_authoritative_oc_payload([a, b], "xray"))
    assert [e["remarks"] for e in xray] == ["A", "B"]


def test_same_host_port_different_credentials_stay_separate():
    a = F.vless("cred-a", "shared.example", 443, remark="One")
    b = F.vless("cred-b", "shared.example", 443, remark="One")
    payload = materialize_authoritative_oc_payload([a, b], "sing_box")
    got = identities_for_format(payload, "sing_box")
    assert len(got) == 2
    assert len({i[3] for i in got}) == 2, "two different credentials must remain two identities"


def test_same_credential_different_hosts_stay_separate():
    a = F.vless("shared-cred", "one.example", 443, remark="One")
    b = F.vless("shared-cred", "two.example", 443, remark="Two")
    got = identities_for_format(materialize_authoritative_oc_payload([a, b], "clash_meta"), "clash_meta")
    assert [(i[1], i[2]) for i in got] == [("one.example", 443), ("two.example", 443)]
    assert got[0][3] == got[1][3]


# --------------------------------------------------------------------------------------------
# unsupported protocols / empty input
# --------------------------------------------------------------------------------------------


def test_hysteria2_skipped_in_structured_formats():
    result = materialize_authoritative_oc_payload_detailed([F.hysteria2("h", "hy.example")], "xray")
    assert result.payload is None
    assert result.input_link_count == 1
    assert result.represented_count == 0
    assert result.skipped_unsupported == 1


def test_unsupported_protocols_do_not_hide_supported_ones():
    links = [F.hysteria2("h", "hy.example"), F.vless("a", "a.example"), F.wireguard("w", "wg.example")]
    result = materialize_authoritative_oc_payload_detailed(links, "xray")
    assert result.input_link_count == 3
    assert result.represented_count == 1
    assert result.skipped_unsupported == 2
    assert identities_for_format(result.payload, "xray") == link_identities([links[1]])


@pytest.mark.parametrize("fmt", ["xray", "sing_box", "clash_meta"])
def test_empty_or_garbage_input_yields_no_payload_never_an_empty_document(fmt):
    for links in ([], [""], ["not a link", "vless://", "vmess://@@@"]):
        result = materialize_authoritative_oc_payload_detailed(links, fmt)
        assert result.payload is None
        assert result.represented_count == 0


# --------------------------------------------------------------------------------------------
# cache key
# --------------------------------------------------------------------------------------------


def _fake_user(**overrides):
    base = {
        "id": 1,
        "expire": None,
        "status": type("S", (), {"value": "active"})(),
        "data_limit": None,
        "inbounds": ("oc_1",),
    }
    base.update(overrides)
    return type("U", (), base)()


def test_cache_keys_differ_by_format():
    from app.subscription.config_cache import make_sub_config_key

    user = _fake_user()
    assert make_sub_config_key(user, "links", False, False, ()) != make_sub_config_key(user, "xray", False, False, ())


def test_cache_key_changes_when_proxy_credentials_change():
    """Rotating a user's proxy credentials must not be served from a stale cached body."""
    from app.subscription.config_cache import make_sub_config_key

    before = make_sub_config_key(_fake_user(proxy_settings={"vless": {"id": "x"}}), "xray", False, False, ())
    after = make_sub_config_key(_fake_user(proxy_settings={"vless": {"id": "y"}}), "xray", False, False, ())
    again = make_sub_config_key(_fake_user(proxy_settings={"vless": {"id": "x"}}), "xray", False, False, ())
    assert before != after
    assert before == again


def test_cache_key_is_stable_across_requests_despite_random_default_credentials():
    """Regression: unset protocols get random defaults per model instance; the key must ignore them."""
    from app.models.proxy import ProxyTable
    from app.subscription.config_cache import make_sub_config_key

    def key(uuid_value):
        settings = ProxyTable.model_validate({"vless": {"id": uuid_value}})
        return make_sub_config_key(_fake_user(proxy_settings=settings), "xray", False, False, ())

    one = "11111111-1111-4111-8111-111111111111"
    two = "22222222-2222-4222-8222-222222222222"
    assert key(one) == key(one), "identical requests must share a cache entry"
    assert key(one) != key(two), "a real credential change must not be served from the old entry"


# --------------------------------------------------------------------------------------------
# real-parser validation (skipped, never faked, when the binaries are absent)
# --------------------------------------------------------------------------------------------

needs_singbox = pytest.mark.skipif(validators.singbox_bin() is None, reason="SINGBOX_BIN not configured")
needs_xray = pytest.mark.skipif(validators.xray_bin() is None, reason="XRAY_BIN not configured")
needs_mihomo = pytest.mark.skipif(validators.mihomo_bin() is None, reason="MIHOMO_BIN not configured")


@needs_singbox
@pytest.mark.parametrize(("case_id", "link", "supported"), [c for c in MATRIX if "sing_box" in c[2]], ids=lambda v: v if isinstance(v, str) and len(v) < 30 else None)
def test_real_singbox_accepts_generated_outbounds(case_id, link, supported):
    payload = materialize_authoritative_oc_payload([link], "sing_box")
    result = validators.check_singbox(payload)
    assert result.ok, result.output


@needs_singbox
def test_real_singbox_rejects_the_old_flat_transport_host_schema():
    """Negative control: proves the harness really catches the Hiddify import error."""
    bad = {
        "outbounds": [
            {
                "type": "vless",
                "tag": "bad",
                "server": "c.example",
                "server_port": 443,
                "uuid": F.cred("c"),
                "transport": {"type": "ws", "path": "/ws", "host": "cdn.c.example"},
            }
        ]
    }
    result = validators.check_singbox(json.dumps(bad))
    assert not result.ok
    assert 'unknown field "host"' in result.output


@needs_xray
def test_real_xray_accepts_generated_entries():
    links = [case[1] for case in MATRIX if "xray" in case[2]]
    payload = materialize_authoritative_oc_payload(links, "xray")
    entries = json.loads(payload)
    results = validators.check_xray(payload)
    assert len(results) == len(entries)
    failures = {
        case_id: result.output
        for (case_id, _l, _s), result in zip([c for c in MATRIX if "xray" in c[2]], results, strict=True)
        if not result.ok and case_id not in XRAY_PARSER_REJECTS
    }
    assert not failures, failures


@needs_mihomo
@pytest.mark.parametrize("fmt", ["clash_meta", "clash"])
def test_real_mihomo_accepts_generated_proxies(fmt):
    links = [case[1] for case in MATRIX if fmt in case[2]]
    payload = materialize_authoritative_oc_payload(links, fmt)
    result = validators.check_mihomo(payload)
    assert result.ok, result.output
