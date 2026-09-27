import asyncio
import datetime
if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc
from datetime import UTC, datetime as dt, timedelta as td
from decimal import Decimal
import sys
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.models.admin import AdminDetails, AdminRoleData
from app.models.admin_role import RolePermissions, CRUDPermissions
from app.db.crud.user import (
    create_user,
    get_user,
    modify_user,
    reset_user_data_usage,
    revoke_user_sub,
)
from app.db.models import (
    Admin,
    AdminRole,
    AdminStatus,
    Group,
    ProxyHost,
    ProxyInbound,
    User,
    UserStatus,
)
from app.db.models_oc import (
    OCIntegration,
    OCPanel,
    OCPanelConfig,
    OCSyncState,
    OCUserMapping,
)
from app.models.settings import ConfigFormat
from app.models.user import UserCreate, UserModify
from app.node.oc_usage import (
    account_mapping_usage,
    fetch_oc_user_usage,
)
from app.node.user import parse_xray_identity
from app.operation import OperatorType
from app.operation.access_control import (
    enforce_user_expirations_now,
    enforce_user_limits_now,
)
from app.operation.subscription import SubscriptionOperation
from app.routers.panel import (
    PanelHostUpdate,
    PanelUpdate,
    get_current_admin_user_context,
    get_current_user_context,
    get_panel,
    get_panel_host,
    sync_panel,
    update_panel,
    update_panel_host,
)


class MockRequest:
    def __init__(self, headers=None, client_host="127.0.0.1", url="http://test/sub"):
        self.headers = headers or {}
        self.client = type("Client", (), {"host": client_host})()
        self.url = url


async def run_tests():
    print("=== Running Phase 15 Security & Authorization Tests ===")
    sub_op = SubscriptionOperation(operator_type=OperatorType.API)
    entities_to_clean = []

    try:
        async with GetDB() as db:
            admin = (await db.execute(select(Admin).limit(1))).scalar_one_or_none()
            assert admin is not None, "Admin record required for tests"

            # =================================================================
            # 1. SUBSCRIPTION SECURITY & TOKEN RESOLUTION AUDIT
            # =================================================================
            print("\n--- 1. Testing Subscription Bearer Token Security ---")

            # 1.1 Unknown and malformed tokens -> 404
            fake_tokens = [
                "nonexistent_token_12345",
                "invalid/token/with/slashes",
                "   ",
                "!@#$%^&*()",
                uuid.uuid4().hex,
            ]
            for bad_tok in fake_tokens:
                try:
                    await sub_op.get_validated_sub(db, bad_tok)
                    assert False, f"Expected 404 for bad token '{bad_tok}'"
                except HTTPException as exc:
                    assert exc.status_code == 404, f"Expected 404, got {exc.status_code}"
            print("   [x] Unknown and malformed subscription tokens reliably reject with 404.")

            # 1.2 Token revocation semantics
            u_sec = User(
                username=f"p15_sec_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                admin_id=admin.id,
                proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            )
            db.add(u_sec)
            await db.flush()
            entities_to_clean.append(u_sec)
            await db.commit()
            await db.refresh(u_sec)

            orig_token = u_sec.sub_token
            assert orig_token is not None and len(orig_token) >= 16

            # Resolves cleanly initially
            resolved_init = await sub_op.get_validated_sub(db, orig_token)
            assert resolved_init.id == u_sec.id

            # Revoke token
            await revoke_user_sub(db, u_sec)
            await db.refresh(u_sec)
            new_token = u_sec.sub_token
            assert new_token != orig_token, "Token must rotate on explicit revocation"

            # Old token must now raise 404
            try:
                await sub_op.get_validated_sub(db, orig_token)
                assert False, "Old token must reject after explicit revocation"
            except HTTPException as exc:
                assert exc.status_code == 404

            # New token resolves cleanly
            resolved_new = await sub_op.get_validated_sub(db, new_token)
            assert resolved_new.id == u_sec.id
            print("   [x] Explicit token revocation invalidates old token (404) and enables new token.")

            # 1.3 Token invariance across non-revoking events
            u_sec.status = UserStatus.limited
            await db.commit()
            await db.refresh(u_sec)
            assert u_sec.sub_token == new_token, "Token must not rotate on status change to limited"

            u_sec.status = UserStatus.expired
            await db.commit()
            await db.refresh(u_sec)
            assert u_sec.sub_token == new_token, "Token must not rotate on status change to expired"

            u_sec.status = UserStatus.disabled
            await db.commit()
            await db.refresh(u_sec)
            assert u_sec.sub_token == new_token, "Token must not rotate on status change to disabled"

            u_sec.status = UserStatus.active
            u_sec.data_limit = 5000000000
            await db.commit()
            await db.refresh(u_sec)
            assert u_sec.sub_token == new_token, "Token must not rotate on restoration"
            print("   [x] Token remains strictly invariant across limits, expirations, and restorations.")

            # =================================================================
            # 2. SUBSCRIPTION RESTRICTION & ACCESS LEAKAGE AUDIT
            # =================================================================
            print("\n--- 2. Testing Subscription Restriction & Zero Config Delivery ---")

            intg_sec = OCIntegration(
                base_url="http://test-sec",
                api_token_encrypted="encrypted",
                token_preview="sec",
                is_active=True,
            )
            db.add(intg_sec)
            await db.flush()
            entities_to_clean.append(intg_sec)

            panel_sec = OCPanel(
                integration_id=intg_sec.id,
                source_panel_id="panel_sec_1",
                purchaser_identity="admin",
                name="Sec Panel",
                multiplier=1.00,
            )
            db.add(panel_sec)
            await db.flush()
            entities_to_clean.append(panel_sec)

            tag_sec = f"oc_{panel_sec.id}_c_sec"
            inbound_sec = ProxyInbound(tag=tag_sec)
            db.add(inbound_sec)
            await db.flush()
            entities_to_clean.append(inbound_sec)

            host_sec = ProxyHost(
                remark="Sec Active Host",
                priority=0,
                address={"1.2.3.4"},
                status=[],
                alpn=[],
                port=443,
                path="/sec",
                allowinsecure=False,
                is_disabled=False,
            )
            host_sec.inbound = inbound_sec
            db.add(host_sec)
            await db.flush()
            entities_to_clean.append(host_sec)

            cfg_sec = OCPanelConfig(
                panel_id=panel_sec.id,
                source_config_id="c_sec",
                source_name="Sec Active Config",
                virtual_inbound_tag=tag_sec,
                source_missing=False,
                protocol="vless",
                network="tcp",
                port=443,
            )
            db.add(cfg_sec)
            entities_to_clean.append(cfg_sec)

            group_sec = Group(name=f"SecGrp_{uuid.uuid4().hex[:6]}", inbounds=[inbound_sec])
            db.add(group_sec)
            await db.flush()
            entities_to_clean.append(group_sec)

            u_sub = User(
                username=f"p15_sub_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=1000,
                used_traffic=100,
                admin_id=admin.id,
                proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            )
            u_sub.groups = [group_sec]
            db.add(u_sub)
            await db.flush()
            entities_to_clean.append(u_sub)
            await db.commit()
            await db.refresh(u_sub)

            # 2.1 Active user -> subscription produces configs
            sub_user_active = await sub_op.validated_user(u_sub)
            assert len(sub_user_active.inbounds) > 0, "Active user must have inbounds"

            req_mock = MockRequest(headers={"User-Agent": "v2rayNG/1.8.5"})
            resp_active = await sub_op.user_subscription(
                db,
                token=u_sub.sub_token,
                user_agent="v2rayNG/1.8.5",
                request_url="http://test/sub",
            )
            active_content = resp_active.body.decode() if isinstance(resp_active.body, bytes) else resp_active.body
            assert len(active_content.strip()) > 0, "Active subscription must deliver configuration content"
            print("   [x] Active user receives populated subscription content.")

            # 2.2 Limited user -> subscription produces ZERO configs
            u_sub.used_traffic = 1500
            await db.commit()
            await enforce_user_limits_now(user_ids=[u_sub.id])
            await db.refresh(u_sub)
            assert u_sub.status == UserStatus.limited

            sub_user_lim = await sub_op.validated_user(u_sub)
            assert sub_user_lim.inbounds == [], "Limited user must have empty inbounds"

            resp_lim = await sub_op.user_subscription(
                db,
                token=u_sub.sub_token,
                user_agent="v2rayNG/1.8.5",
                request_url="http://test/sub",
            )
            lim_content = resp_lim.body.decode() if isinstance(resp_lim.body, bytes) else resp_lim.body
            assert lim_content.strip() == "", "Limited user subscription must contain ZERO proxy configurations"

            # Format-specific endpoint: Clash
            resp_clash = await sub_op.user_subscription_with_client_type(
                db,
                token=u_sub.sub_token,
                client_type=ConfigFormat.clash,
                request_url="http://test/sub/clash",
            )
            clash_content = resp_clash.body.decode() if isinstance(resp_clash.body, bytes) else resp_clash.body
            # Clash output with 0 proxies contains empty proxies list
            assert "proxies: []" in clash_content or "proxies: null" in clash_content or clash_content.strip() == "" or "proxies:" not in clash_content or len([l for l in clash_content.splitlines() if "server:" in l]) == 0

            # Headers must still report valid status and cache control
            assert resp_lim.headers.get("Cache-Control") == "no-store"
            assert "subscription-userinfo" in resp_lim.headers
            assert "download=1500" in resp_lim.headers["subscription-userinfo"]

            # HEAD request returns matching headers
            head_headers = await sub_op.user_subscription_headers(
                db,
                token=u_sub.sub_token,
                user_agent="v2rayNG/1.8.5",
                request_url="http://test/sub",
            )
            assert head_headers.get("Cache-Control") == "no-store"
            assert "download=1500" in head_headers["subscription-userinfo"]

            # Info endpoint returns limited status without leaking secrets
            user_info, info_headers = await sub_op.user_subscription_info(db, token=u_sub.sub_token)
            assert user_info.status == "limited"
            assert user_info.used_traffic == 1500
            assert not hasattr(user_info, "hashed_password")
            assert not hasattr(user_info, "api_token")
            print("   [x] Restricted user gets empty configs across endpoints; headers and info remain accurate without leaking secrets.")

            # 2.3 Restored user -> subscription content restored
            await reset_user_data_usage(db, u_sub)
            await db.refresh(u_sub)
            assert u_sub.status == UserStatus.active

            sub_user_restored = await sub_op.validated_user(u_sub)
            assert len(sub_user_restored.inbounds) > 0, "Restored user must have inbounds restored"

            resp_restored = await sub_op.user_subscription(
                db,
                token=u_sub.sub_token,
                user_agent="v2rayNG/1.8.5",
                request_url="http://test/sub",
            )
            restored_content = resp_restored.body.decode() if isinstance(resp_restored.body, bytes) else resp_restored.body
            assert len(restored_content.strip()) > 0
            assert u_sub.sub_token == sub_user_lim.sub_token, "Subscription token remained stable across restriction/restoration"
            print("   [x] Restored user immediately receives working subscription again with invariant token.")

            # =================================================================
            # 3. AUTHORIZATION & IDOR AUDIT
            # =================================================================
            print("\n--- 3. Testing Authorization & IDOR Isolation ---")

            intg_idor = OCIntegration(
                base_url="http://test-idor",
                api_token_encrypted="encrypted",
                token_preview="idor",
                is_active=True,
            )
            db.add(intg_idor)
            await db.flush()
            entities_to_clean.append(intg_idor)

            panel_a = OCPanel(
                integration_id=intg_idor.id,
                source_panel_id="panel_idor_a",
                purchaser_identity="admin_alice",
                name="Alice Panel",
                multiplier=1.00,
            )
            panel_b = OCPanel(
                integration_id=intg_idor.id,
                source_panel_id="panel_idor_b",
                purchaser_identity="admin_bob",
                name="Bob Panel",
                multiplier=1.00,
            )
            db.add_all([panel_a, panel_b])
            await db.flush()
            entities_to_clean.extend([panel_a, panel_b])

            inb_b = ProxyInbound(tag=f"oc_{panel_b.id}_conf1")
            db.add(inb_b)
            await db.flush()
            entities_to_clean.append(inb_b)

            host_b = ProxyHost(
                remark="Bob Host",
                priority=0,
                address={"5.6.7.8"},
                status=[],
                alpn=[],
                port=443,
                path="/",
                allowinsecure=False,
            )
            host_b.inbound = inb_b
            db.add(host_b)
            await db.flush()
            entities_to_clean.append(host_b)

            conf_b = OCPanelConfig(
                panel_id=panel_b.id,
                source_config_id="conf1",
                source_name="Bob Config",
                virtual_inbound_tag=inb_b.tag,
            )
            db.add(conf_b)
            await db.commit()
            entities_to_clean.append(conf_b)

            alice_ctx = ("admin_alice", False)
            bob_ctx = ("admin_bob", False)

            # 3.1 Admin Alice cannot read or modify Bob's panel
            try:
                await get_panel(panel_id=panel_b.id, db=db, user_context=alice_ctx)
                assert False, "Alice must not be able to read Bob's panel"
            except HTTPException as exc:
                assert exc.status_code == 403
            print("   [x] Cross-tenant panel read blocked with 403 Forbidden.")

            try:
                await update_panel(
                    panel_id=panel_b.id,
                    update_data=PanelUpdate(multiplier=Decimal("3.0")),
                    db=db,
                    user_context=alice_ctx,
                )
                assert False, "Alice must not be able to modify Bob's panel multiplier"
            except HTTPException as exc:
                assert exc.status_code == 403
            print("   [x] Cross-tenant panel modification blocked with 403 Forbidden.")

            # 3.2 Cross-panel host manipulation: cannot modify Bob's host through Alice's panel ID
            try:
                await update_panel_host(
                    panel_id=panel_a.id,
                    host_id=host_b.id,
                    update_data=PanelHostUpdate(display_name="Hacked Host"),
                    db=db,
                    user_context=alice_ctx,
                )
                assert False, "Cannot modify another panel's host through panel_a"
            except HTTPException as exc:
                assert exc.status_code == 404, f"Expected 404 host not in panel, got {exc.status_code}"
            print("   [x] Cross-panel host manipulation strictly blocked (404 host not found in panel).")

            # 3.3 Customer tokens cannot call mutating panel endpoints
            customer_req = MockRequest(headers={"authorization": "Bearer dummy"})
            with patch("app.routers.panel.get_customer_payload", new=AsyncMock(return_value={"account_id": "cust_123"})):
                try:
                    await get_current_admin_user_context(customer_req, db=db, token="dummy")
                    assert False, "Customer accounts must be blocked from mutating panel operations"
                except HTTPException as exc:
                    assert exc.status_code == 403
                    assert "Customer accounts cannot modify" in exc.detail
            print("   [x] Customer accounts are strictly rejected from mutating panel infrastructure.")

            # 3.4 Non-owner admin without nodes:update permission is blocked
            readonly_admin = AdminDetails(
                id=999,
                username="readonly_admin",
                status=AdminStatus.active,
                is_sudo=False,
                role=AdminRoleData(
                    id=999,
                    name="readonly_role",
                    is_owner=False,
                    permissions=RolePermissions(nodes=CRUDPermissions(read=True, update=False)),
                ),
            )
            with patch("app.routers.panel.get_customer_payload", new=AsyncMock(return_value=None)), \
                 patch("app.routers.panel.get_current_for_request", new=AsyncMock(return_value=readonly_admin)):
                try:
                    await get_current_admin_user_context(customer_req, db=db, token="admin_token")
                    assert False, "Readonly admin must be blocked from modifying panels"
                except HTTPException as exc:
                    assert exc.status_code == 403
            print("   [x] Non-owner admins without 'nodes:update' permission are blocked from modifying panels.")

            # =================================================================
            # 4. USAGE ACCOUNTING SECURITY & MALFORMED IDENTITY AUDIT
            # =================================================================
            print("\n--- 4. Testing Identity Parsing Security & Malformed Inputs ---")

            test_cases = [
                ("123", (123, None)),
                (123, (123, None)),
                ("123_p92", (123, 92)),
                ("123_p93", (123, 93)),
                ("123_invalid", None),
                ("abc_p92", None),
                ("123_p", None),
                ("_p92", None),
                ("123_p999999", (123, 999999)),
                (0, None),
                ("0", None),
                ("-100", None),
                ("-123_p92", None),
                ("123_p0", None),
                ("123_p-92", None),
                ("123_p_p92", None),
                ("random_junk_text", None),
            ]
            for raw_input, expected in test_cases:
                result = parse_xray_identity(raw_input)
                assert result == expected, f"Failed for '{raw_input}': expected {expected}, got {result}"
            print("   [x] All valid and adversarial identity patterns parsed safely and accurately.")

            # 4.2 Traffic accounting negative/overflow defense
            mapping_sec = OCUserMapping(
                user_id=u_sub.id,
                panel_id=panel_a.id,
                external_user_id=str(uuid.uuid4()),
                status="active",
                last_cumulative_traffic=1000,
            )
            db.add(mapping_sec)
            await db.commit()
            entities_to_clean.append(mapping_sec)

            # Negative cumulative traffic is safely ignored (returns 0, no DB mutation)
            neg_delta = await account_mapping_usage(mapping_sec.id, remote_cumulative=-500)
            assert neg_delta == 0
            await db.refresh(mapping_sec)
            assert mapping_sec.last_cumulative_traffic == 1000, "Negative cumulative must not mutate baseline"
            print("   [x] Negative cumulative traffic ignored without corrupting baseline.")

            # Upstream reporting huge number (overflow protection)
            huge_resp = {
                "upload_bytes": 10**25,
                "download_bytes": 10**25,
                "total_bytes": 10**26,
            }
            with patch("aiohttp.ClientSession.get") as mock_get:
                mock_resp = AsyncMock()
                mock_resp.status = 200
                mock_resp.json = AsyncMock(return_value=huge_resp)
                mock_get.return_value.__aenter__.return_value = mock_resp

                capped_val = await fetch_oc_user_usage("http://mock", "tok", "p1", "ext1")
                assert capped_val == 9_223_372_036_854_775_807, "Astronomical traffic must cap at BigInteger max"
            print("   [x] Upstream integer overflow safely capped at 64-bit integer limit.")

            # Negative fields in upstream JSON response
            neg_resp = {
                "upload_bytes": -500,
                "download_bytes": -200,
                "total_bytes": -1000,
            }
            with patch("aiohttp.ClientSession.get") as mock_get:
                mock_resp = AsyncMock()
                mock_resp.status = 200
                mock_resp.json = AsyncMock(return_value=neg_resp)
                mock_get.return_value.__aenter__.return_value = mock_resp

                safe_val = await fetch_oc_user_usage("http://mock", "tok", "p1", "ext1")
                assert safe_val == 0, "Negative upstream fields must clamp to 0"
            print("   [x] Negative upstream usage fields safely clamped to 0.")

            # =================================================================
            # 5. OUTBOUND CENTER TOKEN & SYNC HEADER AUDIT
            # =================================================================
            print("\n--- 5. Testing Outbound Center Integration Token Headers ---")

            sync_job = OCSyncState(
                entity_type="user_mapping",
                entity_id=f"{u_sub.id}_{panel_a.id}",
                operation="update",
                idempotency_key=f"sec_sync_{uuid.uuid4()}",
                payload={"configs": ["c1", "c2"]},
                status="pending",
                revision=1,
            )
            db.add(sync_job)
            await db.commit()
            entities_to_clean.append(sync_job)

            captured_headers = {}
            class MockSyncResp:
                def __init__(self, status=200):
                    self.status = status
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass
                def raise_for_status(self):
                    pass

            def mock_put_capture(url, headers=None, json=None, **kwargs):
                captured_headers.update(headers or {})
                return MockSyncResp(200)

            with patch("aiohttp.ClientSession.put", side_effect=mock_put_capture), \
                 patch("app.jobs.process_oc_sync.decrypt_secret", new=AsyncMock(return_value="test_sec_token_999")):
                from app.jobs.process_oc_sync import process_oc_sync
                await process_oc_sync()

            assert captured_headers.get("X-Integration-Token") == "test_sec_token_999", (
                f"Missing X-Integration-Token header in OC sync worker! Headers: {captured_headers}"
            )
            print("   [x] Outbound Center sync worker correctly includes 'X-Integration-Token' header.")

            print("\nALL PHASE 15 SECURITY AUDIT TESTS PASSED SUCCESSFULLY!")

    finally:
        async with GetDB() as db:
            for entity in reversed(entities_to_clean):
                try:
                    await db.delete(entity)
                except Exception:
                    pass
            await db.commit()


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
