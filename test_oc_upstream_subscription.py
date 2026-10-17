"""
OC upstream subscription fetch/merge tests.
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, patch

if not hasattr(__import__("datetime"), "UTC"):
    import datetime

    datetime.UTC = datetime.timezone.utc

from sqlalchemy import select

from app.db import GetDB
from app.db.models import Admin, Group, ProxyHost, ProxyInbound, User, UserStatus
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCUserMapping
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.services.oc_upstream_subscription import (
    merge_subscription_payloads,
    validate_upstream_subscription_url,
)
from app.subscription.config_cache import _cache as sub_cache
from app.subscription.share import generate_subscription


def test_validate_rejects_private_hosts():
    try:
        validate_upstream_subscription_url("https://127.0.0.1/sub/token")
        assert False, "expected rejection"
    except ValueError:
        pass
    try:
        validate_upstream_subscription_url("http://example.com/sub/token")
        assert False, "http must be rejected"
    except ValueError:
        pass


def test_merge_links_preserves_protocol_params():
    native = "vless://native@1.1.1.1:443?encryption=none&security=tls&sni=n.example#Native"
    upstream = "vmess://eyJ2IjoiMiIsInBzIjoiU2VsbGVyIn0=\n"
    merged = merge_subscription_payloads(native, [upstream], "links", as_base64=False)
    assert "vless://native@1.1.1.1:443" in merged
    assert "vmess://" in merged
    assert merged.index("vless://") < merged.index("vmess://")


def test_merge_native_and_upstream_links():
    native = "vless://native@9.9.9.9:443?encryption=none#Native"
    upstream = "vless://seller@94.140.14.14:443?encryption=none&security=reality&pbk=abc#Seller"
    merged = merge_subscription_payloads(native, [upstream], "links", as_base64=False)
    assert "9.9.9.9" in merged and "94.140.14.14" in merged


def test_merge_links_base64_dedupes():
    link = "trojan://user@2.2.2.2:443?sni=t.example#T"
    import base64

    native = base64.b64encode(link.encode()).decode()
    merged = merge_subscription_payloads(native, [native], "links_base64", as_base64=True)
    decoded = base64.b64decode(merged).decode()
    assert decoded.count("trojan://") == 1


async def _setup_oc_user_with_upstream(db, upstream_url: str):
    admin = (await db.execute(select(Admin).limit(1))).scalar_one()
    intg = OCIntegration(
        base_url="https://oc.example",
        api_token_encrypted="e",
        token_preview="t",
    )
    db.add(intg)
    await db.flush()
    panel = OCPanel(
        integration_id=intg.id,
        source_panel_id="sp",
        purchaser_identity="t",
        name="P",
    )
    db.add(panel)
    await db.flush()
    tag = f"oc_{panel.id}_cfg"
    inbound = ProxyInbound(tag=tag)
    db.add(inbound)
    await db.flush()
    host = ProxyHost(
        remark="{USERNAME}",
        priority=1,
        address={"8.8.8.8"},
        port=443,
        path=None,
        allowinsecure=None,
        alpn=[],
        status=[],
        is_disabled=False,
    )
    host.inbound = inbound
    db.add(host)
    await db.flush()
    cfg = OCPanelConfig(
        panel_id=panel.id,
        source_config_id="cfg",
        source_name="Cfg",
        virtual_inbound_tag=tag,
        protocol="vless",
        network="tcp",
        port=443,
    )
    db.add(cfg)
    group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
    db.add(group)
    await db.flush()
    user = User(
        username=f"u_oc_up_{uuid.uuid4().hex[:8]}",
        status=UserStatus.active,
        data_limit=0,
        admin_id=admin.id,
        proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        sub_token=uuid.uuid4().hex,
    )
    user.groups = [group]
    db.add(user)
    await db.flush()
    db.add(
        OCUserMapping(
            user_id=user.id,
            panel_id=panel.id,
            external_user_id="ext",
            status="active",
            last_synced_configs=["cfg"],
            external_subscription_url=upstream_url,
        )
    )
    await db.commit()
    return user.id, panel.id


async def run_tests():
    print("=== OC Upstream Subscription Tests ===\n")
    test_validate_rejects_private_hosts()
    print("   [x] SSRF: private/http rejected")
    test_merge_links_preserves_protocol_params()
    print("   [x] Protocol preservation in links merge")
    test_merge_native_and_upstream_links()
    print("   [x] Native + upstream links merge")
    test_merge_links_base64_dedupes()
    print("   [x] links_base64 dedupe")

    seller_link = "vless://seller@94.140.14.14:443?encryption=none&security=reality&pbk=abc&sid=def#cfg"
    upstream_url = "https://1.1.1.1/sub/seller-token"

    uid = None
    panel_id = None
    async with GetDB() as db:
        uid, panel_id = await _setup_oc_user_with_upstream(db, upstream_url)

    sub_cache.clear()
    sub_op = SubscriptionOperation(operator_type=OperatorType.API)

    async def fake_fetch(url, config_format, user_agent=""):
        return seller_link

    with patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(side_effect=fake_fetch),
    ):
        async with GetDB() as db:
            user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
            validated = await sub_op.validated_user(user)
            out = await generate_subscription(validated, "links", False)

    assert "94.140.14.14" in out, out
    assert "8.8.8.8" not in out, out
    assert "pbk=abc" in out
    print("   [x] OC-only user uses upstream, not synthetic 8.8.8.8")

    sub_cache.clear()
    async with GetDB() as db:
        mapping = (
            await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == panel_id)
            )
        ).scalar_one()
        mapping.external_subscription_url = "https://1.1.1.1/sub/other-token"
        await db.commit()
    with patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(side_effect=TimeoutError("timeout")),
    ):
        async with GetDB() as db:
            user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
            validated = await sub_op.validated_user(user)
            out3 = await generate_subscription(validated, "links", False)
    assert "94.140.14.14" not in out3
    print("   [x] Upstream timeout: failed OC skipped (no synthetic fallback)")

    async with GetDB() as db:
        mapping = (
            await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == panel_id)
            )
        ).scalar_one()
        mapping.status = "deleted"
        mapping.last_synced_configs = []
        await db.commit()

    sub_cache.clear()
    with patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(side_effect=fake_fetch),
    ):
        async with GetDB() as db:
            user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
            validated = await sub_op.validated_user(user)
            out4 = await generate_subscription(validated, "links", False)
    assert "94.140.14.14" not in out4
    print("   [x] Deleted mapping excluded")

    print("   [x] All scenarios covered")


if __name__ == "__main__":
    asyncio.run(run_tests())
    print("\nAll OC upstream subscription tests passed.")
