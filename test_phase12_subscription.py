import asyncio
import sys
import uuid
import datetime
if not hasattr(datetime, 'UTC'):
    datetime.UTC = datetime.timezone.utc
from datetime import UTC, datetime as dt

from fastapi import HTTPException
from sqlalchemy import select, delete

from app.db import GetDB
from app.db.models import User, Group, ProxyInbound, Admin, ProxyHost, UserStatus
from app.db.models_oc import OCUserMapping, OCPanel, OCPanelConfig, OCPanelGroup, OCIntegration
from app.db.crud.admin import build_admin_details
from app.db.crud.user import create_user, create_users_bulk, get_user_by_sub_token
from app.models.user import UserCreate
from app.models.admin import AdminDetails
from app.models.settings import ConfigFormat
from app.operation import OperatorType
from app.operation.user import UserOperation
from app.operation.subscription import SubscriptionOperation
from app.subscription.share import generate_subscription
from app.utils.jwt import create_subscription_token

# Task 4A–4D integration tests may leave users on a shared dev DB with intentionally
# short sub_tokens; they are not part of the Phase 12 migration cohort.
_INTEGRATION_FIXTURE_USERNAME_PREFIXES = ("u4b_", "u4c_", "u4d_", "u4d2_", "u4d-tg_")


def _is_integration_fixture_user(username: str) -> bool:
    return username.startswith(_INTEGRATION_FIXTURE_USERNAME_PREFIXES)


async def run_tests():
    print("=== Running Phase 12 Tests ===")
    user_op = UserOperation(operator_type=OperatorType.API)
    sub_op = SubscriptionOperation(operator_type=OperatorType.API)
    entities_to_clean = []

    try:
        async with GetDB() as db:
            # Get an existing admin
            admin = (await db.execute(select(Admin))).scalars().first()
            assert admin is not None, "Admin must exist in DB"
            admin_details = build_admin_details(admin)

            # ---------------------------------------------------------
            # 1. Stable Token: Migration check, Creation, Stability, Uniqueness
            # ---------------------------------------------------------
            print("--- 1. Testing Stable Token Lifecycle ---")
            
            # Check existing users in DB have sub_token populated from migration
            existing_users = (await db.execute(select(User))).scalars().all()
            assert len(existing_users) > 0, "Users should exist"
            sub_tokens = set()
            for u in existing_users:
                assert u.sub_token is not None, f"User {u.id} sub_token must not be None"
                if not _is_integration_fixture_user(u.username):
                    assert len(u.sub_token) >= 32, f"User {u.id} sub_token should have sufficient length"
                assert u.sub_token not in sub_tokens, f"Duplicate sub_token detected: {u.sub_token}"
                sub_tokens.add(u.sub_token)
            print("   [x] Existing users verified to have unique non-null sub_token from migration.")

            # Create new user via create_user
            u1_name = f"p12_u1_{uuid.uuid4().hex[:8]}"
            u1_create = UserCreate(username=u1_name)
            u1 = await create_user(db, u1_create, [], admin)
            entities_to_clean.append(u1)
            assert u1.sub_token is not None and len(u1.sub_token) == 32
            u1_token = u1.sub_token

            # Bulk user creation
            u2_name = f"p12_u2_{uuid.uuid4().hex[:8]}"
            u3_name = f"p12_u3_{uuid.uuid4().hex[:8]}"
            bulk_users = await create_users_bulk(db, [UserCreate(username=u2_name), UserCreate(username=u3_name)], [], admin)
            entities_to_clean.extend(bulk_users)
            assert len(bulk_users) == 2
            assert bulk_users[0].sub_token != bulk_users[1].sub_token
            assert bulk_users[0].sub_token != u1_token

            # Repeated URL generation must return identical token and URL
            u1_validated = await user_op.validate_user(u1)
            url1 = await UserOperation.generate_subscription_url(u1_validated)
            url2 = await UserOperation.generate_subscription_url(u1_validated)
            url3 = await UserOperation.generate_subscription_url(u1_validated)
            assert url1 == url2 == url3, f"URLs differ: {url1} vs {url2}"
            assert f"/sub/{u1_token}" in url1, f"Expected /sub/{u1_token} in URL, got: {url1}"
            print("   [x] Newly created and bulk users receive unique sub_token; URL generation is stable across calls.")

            # Persistence across fresh DB session
            async with GetDB() as fresh_db:
                reloaded = await get_user_by_sub_token(fresh_db, u1_token)
                assert reloaded is not None and reloaded.id == u1.id
                assert reloaded.sub_token == u1_token
            print("   [x] sub_token persists correctly across DB sessions.")

            # ---------------------------------------------------------
            # 2. Token Resolution: Stable token, unknown token, legacy HMAC
            # ---------------------------------------------------------
            print("--- 2. Testing Token Resolution ---")
            resolved_user = await sub_op.get_validated_sub(db, u1_token)
            assert resolved_user.id == u1.id, "Stable token resolution resolved wrong user"

            # Unknown stable token must return 404
            try:
                await sub_op.get_validated_sub(db, "a" * 32)
                assert False, "Unknown stable token should have raised 404"
            except HTTPException as exc:
                assert exc.status_code == 404
            print("   [x] Unknown token raises 404.")

            # Legacy HMAC token resolution
            legacy_token = await create_subscription_token(u1.id)
            resolved_legacy = await sub_op.get_validated_sub(db, legacy_token)
            assert resolved_legacy.id == u1.id, "Legacy HMAC token failed to resolve user"
            print("   [x] Stable token resolves directly, legacy HMAC backward compatibility preserved.")

            # ---------------------------------------------------------
            # 3. Revocation Semantics
            # ---------------------------------------------------------
            print("--- 3. Testing Revocation Semantics ---")
            # Normal disable must NOT rotate sub_token
            u1.status = UserStatus.disabled
            await db.commit()
            await db.refresh(u1)
            assert u1.sub_token == u1_token, "Normal disable must NOT rotate sub_token"

            # Restore must NOT rotate sub_token
            u1.status = UserStatus.active
            await db.commit()
            await db.refresh(u1)
            assert u1.sub_token == u1_token, "Restore must NOT rotate sub_token"

            # Explicit revoke DOES rotate sub_token
            revoked_resp = await user_op._revoke_user_sub(db, u1, admin_details)
            await db.refresh(u1)
            new_token = u1.sub_token
            assert new_token != u1_token, "Explicit revoke MUST rotate sub_token"
            assert revoked_resp.sub_token == new_token

            # Old token must now 404
            try:
                await sub_op.get_validated_sub(db, u1_token)
                assert False, "Old token must no longer resolve"
            except HTTPException as exc:
                assert exc.status_code == 404

            # New token must resolve
            resolved_new = await sub_op.get_validated_sub(db, new_token)
            assert resolved_new.id == u1.id
            print("   [x] Explicit revoke rotates token, disables old token, enables new token. Disable preserves token.")

            # ---------------------------------------------------------
            # 4. Native & OC Panel Subscription Setup
            # ---------------------------------------------------------
            print("--- 4. Testing Native & Multi-Panel Subscription Generation ---")
            # Create Integration
            integration = OCIntegration(base_url="http://test-p12", api_token_encrypted="encrypted", token_preview="test")
            db.add(integration)
            await db.flush()
            entities_to_clean.append(integration)

            # Panel A
            panel_a = OCPanel(integration_id=integration.id, source_panel_id="panel_A", purchaser_identity="test", name="Panel A")
            db.add(panel_a)
            await db.flush()
            entities_to_clean.append(panel_a)

            # Panel B
            panel_b = OCPanel(integration_id=integration.id, source_panel_id="panel_B", purchaser_identity="test", name="Panel B")
            db.add(panel_b)
            await db.flush()
            entities_to_clean.append(panel_b)

            # Native Inbound N1
            native_inbound = ProxyInbound(tag=f"native_n1_{uuid.uuid4().hex[:8]}")
            db.add(native_inbound)
            await db.flush()
            entities_to_clean.append(native_inbound)

            # Host for native inbound
            native_host = ProxyHost(
                remark="Native Inbound N1",
                priority=1,
                address={"1.1.1.1"},
                port=8443,
                path=None,
                allowinsecure=None,
                alpn=[],
                status=[],
                is_disabled=False,
            )
            native_host.inbound = native_inbound
            db.add(native_host)
            await db.flush()
            entities_to_clean.append(native_host)

            # Virtual Inbound A1 on Panel A
            tag_a1 = f"oc_{panel_a.id}_c1"
            inbound_a1 = ProxyInbound(tag=tag_a1)
            db.add(inbound_a1)
            await db.flush()
            entities_to_clean.append(inbound_a1)

            host_a1 = ProxyHost(
                remark="Panel A Config 1 Remark",
                priority=2,
                address={"2.2.2.2"},
                port=443,
                path=None,
                allowinsecure=None,
                alpn=[],
                status=[],
                is_disabled=False,
            )
            host_a1.inbound = inbound_a1
            db.add(host_a1)
            await db.flush()
            entities_to_clean.append(host_a1)

            cfg_a1 = OCPanelConfig(
                panel_id=panel_a.id,
                source_config_id="c1",
                source_name="Config A1 Original",
                virtual_inbound_tag=tag_a1,
                source_missing=False,
                protocol="vless",
                network="tcp",
                port=443,
            )
            db.add(cfg_a1)
            entities_to_clean.append(cfg_a1)

            # Virtual Inbound A2 on Panel A (NOT assigned to user's group)
            tag_a2 = f"oc_{panel_a.id}_c2"
            inbound_a2 = ProxyInbound(tag=tag_a2)
            db.add(inbound_a2)
            await db.flush()
            entities_to_clean.append(inbound_a2)

            host_a2 = ProxyHost(
                remark="Panel A Config 2 (Unauthorized)",
                priority=3,
                address={"2.2.2.3"},
                port=443,
                path=None,
                allowinsecure=None,
                alpn=[],
                status=[],
                is_disabled=False,
            )
            host_a2.inbound = inbound_a2
            db.add(host_a2)
            await db.flush()
            entities_to_clean.append(host_a2)

            cfg_a2 = OCPanelConfig(
                panel_id=panel_a.id,
                source_config_id="c2",
                source_name="Config A2 Unauthorized",
                virtual_inbound_tag=tag_a2,
                source_missing=False,
                protocol="vless",
                network="tcp",
                port=443,
            )
            db.add(cfg_a2)
            entities_to_clean.append(cfg_a2)

            # Virtual Inbound B1 on Panel B
            tag_b1 = f"oc_{panel_b.id}_c1"
            inbound_b1 = ProxyInbound(tag=tag_b1)
            db.add(inbound_b1)
            await db.flush()
            entities_to_clean.append(inbound_b1)

            host_b1 = ProxyHost(
                remark="Panel B Config 1 Remark",
                priority=4,
                address={"3.3.3.3"},
                port=443,
                path=None,
                allowinsecure=None,
                alpn=[],
                status=[],
                is_disabled=False,
            )
            host_b1.inbound = inbound_b1
            db.add(host_b1)
            await db.flush()
            entities_to_clean.append(host_b1)

            cfg_b1 = OCPanelConfig(
                panel_id=panel_b.id,
                source_config_id="c1",
                source_name="Config B1",
                virtual_inbound_tag=tag_b1,
                source_missing=False,
                protocol="vless",
                network="tcp",
                port=443,
            )
            db.add(cfg_b1)
            entities_to_clean.append(cfg_b1)

            # Group 1: has Native N1 and Panel A Config 1 (inbound_a1)
            grp1 = Group(name=f"Group 1_{uuid.uuid4().hex[:6]}", inbounds=[native_inbound, inbound_a1])
            db.add(grp1)
            await db.flush()
            entities_to_clean.append(grp1)

            # Group 2: has Panel B Config 1 (inbound_b1)
            grp2 = Group(name=f"Group 2_{uuid.uuid4().hex[:6]}", inbounds=[inbound_b1])
            db.add(grp2)
            await db.flush()
            entities_to_clean.append(grp2)

            # Group 3 (unassigned group): has Panel A Config 2 (inbound_a2)
            grp3 = Group(name=f"Group 3_{uuid.uuid4().hex[:6]}", inbounds=[inbound_a2])
            db.add(grp3)
            await db.flush()
            entities_to_clean.append(grp3)

            # User test_sub with vless proxy_settings
            vless_uuid = str(uuid.uuid4())
            sub_user = User(
                username=f"p12_sub_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=0,
                admin_id=admin.id,
                proxy_settings={"vless": {"id": vless_uuid}},
            )
            # Initially assign to Group 1 only (has native N1 and Panel A Config 1)
            sub_user.groups = [grp1]
            db.add(sub_user)
            await db.flush()
            entities_to_clean.append(sub_user)

            # Also add an existing OCUserMapping to verify it is NEVER modified by subscription rendering
            mapping_a = OCUserMapping(
                user_id=sub_user.id,
                panel_id=panel_a.id,
                external_user_id="ext_user_a",
                last_cumulative_traffic=1000,
                status="active",
                last_synced_configs=["c1"],
            )
            mapping_b = OCUserMapping(
                user_id=sub_user.id,
                panel_id=panel_b.id,
                external_user_id="ext_user_b",
                status="active",
                last_synced_configs=["c1"],
            )
            db.add_all([mapping_a, mapping_b])
            await db.flush()
            entities_to_clean.extend([mapping_a, mapping_b])

            await db.commit()
            await db.refresh(sub_user)

            # Generate subscription for user in Group 1
            validated_sub_user = await sub_op.validated_user(sub_user)
            sub_output = await generate_subscription(validated_sub_user, "links", False)
            
            # Verify:
            # 1) Panel A Config 1 (host_a1 remark or address 2.2.2.2) IS present
            assert "2.2.2.2" in sub_output or "Panel A Config 1 Remark" in sub_output, "Config A1 should appear in subscription"
            # 2) Panel A Config 2 (unauthorized) MUST NOT be present
            assert "2.2.2.3" not in sub_output and "Panel A Config 2" not in sub_output, "Config A2 MUST NOT appear (unauthorized group)"
            # 3) Panel B Config 1 (not in user groups) MUST NOT be present
            assert "3.3.3.3" not in sub_output and "Panel B Config 1" not in sub_output, "Config B1 MUST NOT appear (Group 2 not assigned)"
            print("   [x] Subscription contains only configs from user's assigned group (Panel A1 present, unauthorized A2 and B1 absent).")

            # Now add Group 2 to sub_user
            await sub_user.awaitable_attrs.groups
            sub_user.groups = [grp1, grp2]
            await db.commit()
            await db.refresh(sub_user, ["groups"])

            validated_sub_user2 = await sub_op.validated_user(sub_user)
            sub_output2 = await generate_subscription(validated_sub_user2, "links", False)
            assert ("2.2.2.2" in sub_output2 or "Panel A Config 1 Remark" in sub_output2), "Config A1 should appear"
            assert ("3.3.3.3" in sub_output2 or "Panel B Config 1 Remark" in sub_output2), "Config B1 should appear now that Group 2 is assigned"
            assert "2.2.2.3" not in sub_output2 and "Panel A Config 2" not in sub_output2, "Config A2 MUST still be absent"
            print("   [x] Multi-panel aggregation verified: Panel A1 and Panel B1 both present, A2 still absent.")

            # ---------------------------------------------------------
            # 5. Config Lifecycle: source_missing = True / False
            # ---------------------------------------------------------
            print("--- 5. Testing OC Config Lifecycle (source_missing) ---")
            from app.subscription.config_cache import _cache
            _cache.clear()
            saved_token = sub_user.sub_token

            # Mark cfg_a1 as source_missing = True
            cfg_a1.source_missing = True
            await db.commit()
            _cache.clear()

            validated_sub_user = await sub_op.validated_user(sub_user)
            sub_output_missing = await generate_subscription(validated_sub_user, "links", False)
            assert "2.2.2.2" not in sub_output_missing and "Panel A Config 1 Remark" not in sub_output_missing, "Missing config must NOT appear in subscription"
            assert ("3.3.3.3" in sub_output_missing or "Panel B Config 1 Remark" in sub_output_missing), "Config B1 should still appear"

            # Reappear: source_missing = False
            cfg_a1.source_missing = False
            await db.commit()
            _cache.clear()

            validated_sub_user = await sub_op.validated_user(sub_user)
            sub_output_reappear = await generate_subscription(validated_sub_user, "links", False)
            assert ("2.2.2.2" in sub_output_reappear or "Panel A Config 1 Remark" in sub_output_reappear), "Reappeared config must appear in subscription"
            assert sub_user.sub_token == saved_token, "Subscription token must remain unchanged throughout config lifecycle"
            print("   [x] source_missing=True disappears, source_missing=False reappears, token unchanged.")

            # ---------------------------------------------------------
            # 6. Host Lifecycle: is_disabled = True / False
            # ---------------------------------------------------------
            print("--- 6. Testing Host Lifecycle (is_disabled) ---")
            host_b1.is_disabled = True
            await db.commit()
            _cache.clear()

            validated_sub_user = await sub_op.validated_user(sub_user)
            sub_output_host_disabled = await generate_subscription(validated_sub_user, "links", False)
            assert "3.3.3.3" not in sub_output_host_disabled and "Panel B Config 1 Remark" not in sub_output_host_disabled, "Disabled host must NOT appear in subscription"

            # Re-enable host
            host_b1.is_disabled = False
            await db.commit()
            _cache.clear()

            validated_sub_user = await sub_op.validated_user(sub_user)
            sub_output_host_enabled = await generate_subscription(validated_sub_user, "links", False)
            assert ("3.3.3.3" in sub_output_host_enabled or "Panel B Config 1 Remark" in sub_output_host_enabled), "Re-enabled host must appear in subscription"
            print("   [x] Disabled host excluded, re-enabled host included.")

            # ---------------------------------------------------------
            # 7. Mapping Preservation
            # ---------------------------------------------------------
            print("--- 7. Testing Mapping Preservation ---")
            mapping_check = (await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == sub_user.id, OCUserMapping.panel_id == panel_a.id)
            )).scalar_one()
            assert mapping_check.external_user_id == "ext_user_a", "Mapping external_user_id was altered!"
            assert mapping_check.last_cumulative_traffic == 1000, "Mapping last_cumulative_traffic was altered!"
            print("   [x] OCUserMapping remained completely untouched and unmutated by subscription generation.")

            # ---------------------------------------------------------
            # 8. Diverse Clients (Clash, Clash Meta, Sing-box, Xray)
            # ---------------------------------------------------------
            print("--- 8. Testing Formats (Clash, Sing-box, Xray) ---")
            clash_out = await generate_subscription(validated_sub_user, "clash", False)
            assert clash_out is not None and len(clash_out) > 0, "Clash output should not be empty"

            clash_meta_out = await generate_subscription(validated_sub_user, "clash_meta", False)
            assert clash_meta_out is not None and len(clash_meta_out) > 0, "Clash Meta output should not be empty"

            singbox_out = await generate_subscription(validated_sub_user, "sing_box", False)
            assert singbox_out is not None and len(singbox_out) > 0, "Sing-box output should not be empty"

            xray_out = await generate_subscription(validated_sub_user, "xray", False)
            assert xray_out is not None and len(xray_out) > 0, "Xray output should not be empty"
            print("   [x] Multiple subscription formats render successfully.")

            print("\nALL PHASE 12 TESTS PASSED SUCCESSFULLY!")

    finally:
        # Clean up created entities safely
        async with GetDB() as db:
            for entity in reversed(entities_to_clean):
                try:
                    await db.delete(entity)
                except Exception:
                    pass
            await db.commit()


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
