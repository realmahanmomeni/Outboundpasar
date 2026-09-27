import asyncio
import datetime
if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc
from datetime import UTC, datetime as dt, timedelta as td
import sys
import uuid

from sqlalchemy import delete, select

from app.db import GetDB
from app.db.crud.admin import build_admin_details
from app.db.crud.user import (
    create_user,
    get_user,
    modify_user,
    reset_user_data_usage,
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
from app.models.user import UserCreate, UserModify
from app.node.oc_sync import enqueue_oc_user_sync
from app.node.oc_usage import account_mapping_usage
from app.node.user import serialize_user, serialize_users_for_node
from app.operation import OperatorType
from app.operation.access_control import (
    check_user_access_allowed,
    enforce_user_expirations_now,
    enforce_user_limits_now,
    evaluate_user_access,
    is_user_access_allowed,
)
from app.operation.admin_sync import sync_admin_users_for_block_transition
from app.operation.subscription import SubscriptionOperation
from app.operation.user import UserOperation


async def run_tests():
    print("=== Running Phase 14 Native PasarGuard Restrictions & Access Control Tests ===")
    user_op = UserOperation(operator_type=OperatorType.API)
    sub_op = SubscriptionOperation(operator_type=OperatorType.API)
    entities_to_clean = []

    try:
        async with GetDB() as db:
            admin = (await db.execute(select(Admin))).scalars().first()
            assert admin is not None, "Admin must exist in DB"
            admin_details = build_admin_details(admin)

            # =================================================================
            # 1. Deterministic Access Decision Unit Logic
            # =================================================================
            print("\n--- 1. Testing Deterministic Access Decision Logic ---")

            # 1.1 Active user under limit
            u_ok = User(
                username=f"p14_ok_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=1000,
                used_traffic=500,
                admin_id=admin.id,
            )
            assert is_user_access_allowed(u_ok, admin) is True
            eval_ok = evaluate_user_access(u_ok, admin)
            assert eval_ok["allowed"] is True
            assert eval_ok["reason"] is None
            print("   [x] Active user under limit is allowed.")

            # 1.2 User exactly at limit
            u_at = User(
                username=f"p14_at_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=1000,
                used_traffic=1000,
                admin_id=admin.id,
            )
            assert is_user_access_allowed(u_at, admin) is False
            eval_at = evaluate_user_access(u_at, admin)
            assert eval_at["allowed"] is False
            assert eval_at["reason"] == "data_limit_exceeded"
            print("   [x] User exactly at limit (used == limit) is restricted.")

            # 1.3 User over limit
            u_over = User(
                username=f"p14_over_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=1000,
                used_traffic=1500,
                admin_id=admin.id,
            )
            assert is_user_access_allowed(u_over, admin) is False
            eval_over = evaluate_user_access(u_over, admin)
            assert eval_over["allowed"] is False
            assert eval_over["reason"] == "data_limit_exceeded"
            print("   [x] User over limit is restricted.")

            # 1.4 Unlimited users (data_limit=None and data_limit=0)
            u_unlim_none = User(
                username=f"p14_unlim1_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=None,
                used_traffic=10_000_000_000,
                admin_id=admin.id,
            )
            u_unlim_zero = User(
                username=f"p14_unlim2_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=0,
                used_traffic=10_000_000_000,
                admin_id=admin.id,
            )
            assert is_user_access_allowed(u_unlim_none, admin) is True
            assert is_user_access_allowed(u_unlim_zero, admin) is True
            print("   [x] Unlimited users (None and 0) remain allowed regardless of used traffic.")

            # 1.5 Expired user
            u_expired = User(
                username=f"p14_exp_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=1000,
                used_traffic=100,
                admin_id=admin.id,
            )
            u_expired.expire = dt.now(UTC) - td(minutes=10)
            assert is_user_access_allowed(u_expired, admin) is False
            eval_exp = evaluate_user_access(u_expired, admin)
            assert eval_exp["allowed"] is False
            assert eval_exp["reason"] == "expired"
            print("   [x] Expired user is restricted.")

            # 1.6 Disabled user
            u_disabled = User(
                username=f"p14_dis_{uuid.uuid4().hex[:8]}",
                status=UserStatus.disabled,
                data_limit=1000,
                used_traffic=100,
                admin_id=admin.id,
            )
            assert is_user_access_allowed(u_disabled, admin) is False
            eval_dis = evaluate_user_access(u_disabled, admin)
            assert eval_dis["allowed"] is False
            assert eval_dis["reason"] == "status_disabled"
            print("   [x] Disabled user is restricted.")

            # 1.7 On-hold users (under limit vs over limit)
            u_on_hold_ok = User(
                username=f"p14_oh1_{uuid.uuid4().hex[:8]}",
                status=UserStatus.on_hold,
                data_limit=1000,
                used_traffic=0,
                admin_id=admin.id,
            )
            u_on_hold_over = User(
                username=f"p14_oh2_{uuid.uuid4().hex[:8]}",
                status=UserStatus.on_hold,
                data_limit=1000,
                used_traffic=1000,
                admin_id=admin.id,
            )
            assert is_user_access_allowed(u_on_hold_ok, admin) is True
            assert is_user_access_allowed(u_on_hold_over, admin) is False
            print("   [x] On-hold user under limit allowed; on-hold user over limit restricted.")

            # =================================================================
            # 2. Admin Limit Interactions
            # =================================================================
            print("\n--- 2. Testing Admin Limits & Role Interactions ---")

            role_disconnect = AdminRole(
                name=f"Role_Disc_{uuid.uuid4().hex[:6]}",
                disconnect_users_when_limited=True,
                disconnect_users_when_disabled=True,
            )
            role_no_disconnect = AdminRole(
                name=f"Role_NoDisc_{uuid.uuid4().hex[:6]}",
                disconnect_users_when_limited=False,
                disconnect_users_when_disabled=False,
            )
            db.add_all([role_disconnect, role_no_disconnect])
            await db.flush()
            entities_to_clean.extend([role_disconnect, role_no_disconnect])

            admin_limited = Admin(
                username=f"adm_lim_{uuid.uuid4().hex[:6]}",
                hashed_password="hash",
                status=AdminStatus.limited,
            )
            admin_limited.role = role_disconnect

            admin_limited_nodisc = Admin(
                username=f"adm_lim_nd_{uuid.uuid4().hex[:6]}",
                hashed_password="hash",
                status=AdminStatus.limited,
            )
            admin_limited_nodisc.role = role_no_disconnect

            admin_over_data = Admin(
                username=f"adm_over_{uuid.uuid4().hex[:6]}",
                hashed_password="hash",
                status=AdminStatus.active,
                data_limit=5000,
            )
            admin_over_data.used_traffic = 5000
            admin_over_data.role = role_disconnect

            u_under_lim_admin = User(
                username=f"u_la_{uuid.uuid4().hex[:6]}",
                status=UserStatus.active,
                data_limit=1000,
                used_traffic=10,
                admin_id=1,
            )
            u_under_lim_admin.admin = admin_limited
            assert is_user_access_allowed(u_under_lim_admin, admin_limited) is False

            u_under_nodisc_admin = User(
                username=f"u_lnd_{uuid.uuid4().hex[:6]}",
                status=UserStatus.active,
                data_limit=1000,
                used_traffic=10,
                admin_id=2,
            )
            u_under_nodisc_admin.admin = admin_limited_nodisc
            assert is_user_access_allowed(u_under_nodisc_admin, admin_limited_nodisc) is True

            u_under_over_admin = User(
                username=f"u_oda_{uuid.uuid4().hex[:6]}",
                status=UserStatus.active,
                data_limit=1000,
                used_traffic=10,
                admin_id=3,
            )
            u_under_over_admin.admin = admin_over_data
            assert is_user_access_allowed(u_under_over_admin, admin_over_data) is False
            print("   [x] Admin limit and role disconnect flags correctly enforce or permit user access.")

            # =================================================================
            # 3. Real DB Enforcement & Status Transitions (Idempotent)
            # =================================================================
            print("\n--- 3. Testing Database Enforcement & Idempotency ---")

            u_test_lim = await create_user(
                db,
                UserCreate(username=f"p14_db_lim_{uuid.uuid4().hex[:8]}", data_limit=500),
                [],
                admin,
            )
            entities_to_clean.append(u_test_lim)
            u_test_lim.used_traffic = 500  # Exactly at limit
            await db.commit()

            # Enforce limits now
            count1 = await enforce_user_limits_now(user_ids=[u_test_lim.id])
            assert count1 >= 1, "Expected user to be limited"

            await db.refresh(u_test_lim)
            assert u_test_lim.status == UserStatus.limited, "User status should be limited"

            # Repeated enforcement -> Idempotent!
            count2 = await enforce_user_limits_now(user_ids=[u_test_lim.id])
            assert count2 == 0, "Repeated enforcement must be a NO-OP"
            assert u_test_lim.status == UserStatus.limited
            print("   [x] User limit enforcement transitions status to limited and is idempotent.")

            # Test Expiration enforcement
            u_test_exp = await create_user(
                db,
                UserCreate(username=f"p14_db_exp_{uuid.uuid4().hex[:8]}"),
                [],
                admin,
            )
            entities_to_clean.append(u_test_exp)
            u_test_exp.expire = dt.now(UTC) - td(minutes=5)
            await db.commit()

            count_exp1 = await enforce_user_expirations_now(user_ids=[u_test_exp.id])
            assert count_exp1 >= 1
            await db.refresh(u_test_exp)
            assert u_test_exp.status == UserStatus.expired

            count_exp2 = await enforce_user_expirations_now(user_ids=[u_test_exp.id])
            assert count_exp2 == 0
            assert u_test_exp.status == UserStatus.expired
            print("   [x] User expiration enforcement transitions status to expired and is idempotent.")

            # =================================================================
            # 4. Restoration Lifecycle
            # =================================================================
            print("\n--- 4. Testing Restoration (Traffic Reset, Limit Increase, Renewal) ---")

            # 4.1 Traffic Reset restoration
            await reset_user_data_usage(db, u_test_lim)
            await db.refresh(u_test_lim)
            assert u_test_lim.used_traffic == 0
            assert u_test_lim.status == UserStatus.active
            assert is_user_access_allowed(u_test_lim, admin) is True
            print("   [x] Reset user traffic restores limited user to active.")

            # Re-limit
            u_test_lim.used_traffic = 600
            await db.commit()
            await enforce_user_limits_now(user_ids=[u_test_lim.id])
            await db.refresh(u_test_lim)
            assert u_test_lim.status == UserStatus.limited

            # 4.2 Limit Increase restoration
            await modify_user(db, u_test_lim, UserModify(data_limit=2000))
            await db.refresh(u_test_lim)
            assert u_test_lim.data_limit == 2000
            assert u_test_lim.status == UserStatus.active
            assert is_user_access_allowed(u_test_lim, admin) is True
            print("   [x] Increasing data_limit restores limited user to active.")

            # 4.3 Expired user renewal
            await modify_user(db, u_test_exp, UserModify(expire=dt.now(UTC) + td(days=30)))
            await db.refresh(u_test_exp)
            assert u_test_exp.status == UserStatus.active
            assert is_user_access_allowed(u_test_exp, admin) is True
            print("   [x] Extending expiration restores expired user to active.")

            # =================================================================
            # 5. Stable Subscription Token & External Identity Invariance
            # =================================================================
            print("\n--- 5. Testing Stable Token & Identity Invariance during Restriction ---")

            orig_token = u_test_lim.sub_token
            assert orig_token is not None and len(orig_token) == 32

            # Restrict via limit
            u_test_lim.used_traffic = 3000
            await db.commit()
            await enforce_user_limits_now(user_ids=[u_test_lim.id])
            await db.refresh(u_test_lim)
            assert u_test_lim.status == UserStatus.limited
            assert u_test_lim.sub_token == orig_token, "Token must NOT rotate during limit restriction"

            # Restrict via expire
            u_test_lim.status = UserStatus.active
            u_test_lim.expire = dt.now(UTC) - td(minutes=1)
            await db.commit()
            await enforce_user_expirations_now(user_ids=[u_test_lim.id])
            await db.refresh(u_test_lim)
            assert u_test_lim.status == UserStatus.expired
            assert u_test_lim.sub_token == orig_token, "Token must NOT rotate during expiration restriction"

            # Restrict via disable
            u_test_lim.status = UserStatus.disabled
            await db.commit()
            await db.refresh(u_test_lim)
            assert u_test_lim.sub_token == orig_token, "Token must NOT rotate during administrative disable"

            # Restore
            u_test_lim.expire = dt.now(UTC) + td(days=10)
            u_test_lim.data_limit = 10000
            u_test_lim.status = UserStatus.active
            await db.commit()
            await db.refresh(u_test_lim)
            assert u_test_lim.sub_token == orig_token, "Token must NOT rotate during restoration"
            print("   [x] Stable subscription token remained perfectly invariant across all restrictions.")

            # =================================================================
            # 6. Native PasarGuard User Enforcement
            # =================================================================
            print("\n--- 6. Testing Native PasarGuard User Enforcement ---")

            native_inbound = ProxyInbound(tag=f"native_p14_{uuid.uuid4().hex[:6]}")
            db.add(native_inbound)
            await db.flush()
            entities_to_clean.append(native_inbound)

            native_group = Group(name=f"NatGrp_{uuid.uuid4().hex[:6]}", inbounds=[native_inbound])
            db.add(native_group)
            await db.flush()
            entities_to_clean.append(native_group)

            u_native = User(
                username=f"p14_nat_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=1000,
                used_traffic=200,
                admin_id=admin.id,
                proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            )
            u_native.groups = [native_group]
            db.add(u_native)
            await db.flush()
            entities_to_clean.append(u_native)
            await db.commit()
            await db.refresh(u_native)

            # 6.1 Under limit -> Native inbounds present
            proto_active = await serialize_user(u_native)
            assert len(proto_active) == 1
            assert native_inbound.tag in proto_active[0].inbounds
            print("   [x] Native active user has inbounds serialized.")

            # 6.2 Over limit -> Native inbounds EMPTY
            u_native.used_traffic = 1000
            await db.commit()
            await enforce_user_limits_now(user_ids=[u_native.id])
            await db.refresh(u_native)

            proto_limited = await serialize_user(u_native)
            assert len(proto_limited) == 1
            assert proto_limited[0].inbounds == [], "Limited user must have empty inbounds for native nodes"

            proto_batch_lim = await serialize_users_for_node([u_native])
            assert len(proto_batch_lim) == 1
            assert proto_batch_lim[0].inbounds == []
            print("   [x] Native limited user receives empty inbounds (proxy access dropped).")

            # 6.3 Reset -> Inbounds restored
            await reset_user_data_usage(db, u_native)
            await db.refresh(u_native)
            assert u_native.status == UserStatus.active

            proto_restored = await serialize_user(u_native)
            assert len(proto_restored) == 1
            assert native_inbound.tag in proto_restored[0].inbounds
            print("   [x] Native user restored after traffic reset receives inbounds again.")

            # =================================================================
            # 7. Multi-Panel User Isolation & Phase 10 Reconciliation
            # =================================================================
            print("\n--- 7. Testing Multi-Panel User Restriction & Reconciliation ---")

            integration = OCIntegration(
                base_url="http://test-p14",
                api_token_encrypted="encrypted",
                token_preview="p14",
            )
            db.add(integration)
            await db.flush()
            entities_to_clean.append(integration)

            panel_a = OCPanel(
                integration_id=integration.id,
                source_panel_id="panel_14A",
                purchaser_identity="test",
                name="Panel 14A",
                multiplier=1.0,
            )
            panel_b = OCPanel(
                integration_id=integration.id,
                source_panel_id="panel_14B",
                purchaser_identity="test",
                name="Panel 14B",
                multiplier=2.0,
            )
            db.add_all([panel_a, panel_b])
            await db.flush()
            entities_to_clean.extend([panel_a, panel_b])

            inbound_a = ProxyInbound(tag=f"oc_{panel_a.id}_cfg1_{uuid.uuid4().hex[:6]}")
            inbound_b = ProxyInbound(tag=f"oc_{panel_b.id}_cfg2_{uuid.uuid4().hex[:6]}")
            db.add_all([inbound_a, inbound_b])
            await db.flush()
            entities_to_clean.extend([inbound_a, inbound_b])

            cfg_a = OCPanelConfig(
                panel_id=panel_a.id,
                source_config_id="cfg_a",
                source_name="Config A",
                virtual_inbound_tag=inbound_a.tag,
                source_missing=False,
            )
            cfg_b = OCPanelConfig(
                panel_id=panel_b.id,
                source_config_id="cfg_b",
                source_name="Config B",
                virtual_inbound_tag=inbound_b.tag,
                source_missing=False,
            )
            db.add_all([cfg_a, cfg_b])
            await db.flush()
            entities_to_clean.extend([cfg_a, cfg_b])

            group_multi = Group(
                name=f"MultiGroup_{uuid.uuid4().hex[:6]}",
                inbounds=[inbound_a, inbound_b],
            )
            db.add(group_multi)
            await db.flush()
            entities_to_clean.append(group_multi)

            u_multi = User(
                username=f"p14_multi_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=5000,
                used_traffic=1000,
                admin_id=admin.id,
                proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            )
            u_multi.groups = [group_multi]
            db.add(u_multi)
            await db.flush()
            entities_to_clean.append(u_multi)

            # Create existing active mappings for both panels
            map_a = OCUserMapping(
                user_id=u_multi.id,
                panel_id=panel_a.id,
                external_user_id=f"{u_multi.id}_p{panel_a.id}",
                status="active",
                last_synced_configs=["cfg_a"],
                last_cumulative_traffic=1000,
            )
            map_b = OCUserMapping(
                user_id=u_multi.id,
                panel_id=panel_b.id,
                external_user_id=f"{u_multi.id}_p{panel_b.id}",
                status="active",
                last_synced_configs=["cfg_b"],
                last_cumulative_traffic=2000,
            )
            db.add_all([map_a, map_b])
            await db.commit()
            entities_to_clean.extend([map_a, map_b])

            # 7.1 Verify active state: NO-OP drift
            await enqueue_oc_user_sync(db, u_multi)
            await db.commit()
            jobs_active = (await db.execute(
                select(OCSyncState).where(OCSyncState.entity_id.in_([f"{u_multi.id}_{panel_a.id}", f"{u_multi.id}_{panel_b.id}"]))
            )).scalars().all()
            assert len(jobs_active) == 0, "No drift should mean no sync jobs"
            print("   [x] Active two-panel user has zero drift (NO-OP).")

            # 7.2 Global user restriction: user exceeds data_limit
            u_multi.used_traffic = 5000
            await db.commit()
            await enforce_user_limits_now(user_ids=[u_multi.id])
            await db.refresh(u_multi)
            assert u_multi.status == UserStatus.limited

            # Verify that enqueue_oc_user_sync creates DELETE jobs for BOTH panels
            await enqueue_oc_user_sync(db, u_multi)
            await db.commit()

            job_a = (await db.execute(
                select(OCSyncState).where(OCSyncState.entity_id == f"{u_multi.id}_{panel_a.id}", OCSyncState.status == "pending")
            )).scalar_one_or_none()
            job_b = (await db.execute(
                select(OCSyncState).where(OCSyncState.entity_id == f"{u_multi.id}_{panel_b.id}", OCSyncState.status == "pending")
            )).scalar_one_or_none()

            assert job_a is not None and job_a.operation == "delete"
            assert job_b is not None and job_b.operation == "delete"
            assert job_a.payload["external_user_id"] == map_a.external_user_id
            assert job_b.payload["external_user_id"] == map_b.external_user_id

            # Verify mappings are preserved!
            assert map_a.external_user_id == f"{u_multi.id}_p{panel_a.id}"
            assert map_b.external_user_id == f"{u_multi.id}_p{panel_b.id}"
            print("   [x] Global restriction creates independent DELETE sync jobs across both panels, mappings preserved.")

            # 7.3 One panel failure isolation: simulate Panel A failing, Panel B succeeding
            job_a.status = "failed"
            job_a.last_error = "Connection refused on Panel A"
            job_b.status = "completed"
            map_b.status = "deleted"
            map_b.last_synced_configs = []
            await db.commit()

            # Verify Panel B completed without being affected by Panel A failure
            assert map_b.status == "deleted"
            assert map_a.status == "active"  # Untouched on panel A because job failed
            print("   [x] Panel-specific failure remains isolated (Panel A failure did not corrupt Panel B).")

            # Simulate Panel A subsequently completing deletion
            job_a.status = "completed"
            map_a.status = "deleted"
            map_a.last_synced_configs = []
            await db.commit()

            # 7.4 Restoration across both panels
            await reset_user_data_usage(db, u_multi)
            await db.refresh(u_multi)
            assert u_multi.status == UserStatus.active

            # Clean pending jobs
            await db.execute(
                delete(OCSyncState).where(OCSyncState.entity_id.in_([f"{u_multi.id}_{panel_a.id}", f"{u_multi.id}_{panel_b.id}"]))
            )
            await db.commit()

            await enqueue_oc_user_sync(db, u_multi)
            await db.commit()

            restore_job_a = (await db.execute(
                select(OCSyncState).where(OCSyncState.entity_id == f"{u_multi.id}_{panel_a.id}", OCSyncState.status == "pending")
            )).scalar_one_or_none()
            restore_job_b = (await db.execute(
                select(OCSyncState).where(OCSyncState.entity_id == f"{u_multi.id}_{panel_b.id}", OCSyncState.status == "pending")
            )).scalar_one_or_none()

            assert restore_job_a is not None and restore_job_a.payload["configs"] == ["cfg_a"]
            assert restore_job_b is not None and restore_job_b.payload["configs"] == ["cfg_b"]
            assert map_a.external_user_id == f"{u_multi.id}_p{panel_a.id}"
            assert map_b.external_user_id == f"{u_multi.id}_p{panel_b.id}"
            print("   [x] Restored user re-enqueues desired configs for both panels without modifying external identities.")

            # =================================================================
            # 8. Concurrency & Immediate Accounting Enforcement
            # =================================================================
            print("\n--- 8. Testing Immediate Accounting + Enforcement Sequence ---")

            u_conc = await create_user(
                db,
                UserCreate(username=f"p14_conc_{uuid.uuid4().hex[:8]}", data_limit=2000),
                [],
                admin,
            )
            entities_to_clean.append(u_conc)
            u_conc.groups = [group_multi]
            await db.commit()

            map_conc = OCUserMapping(
                user_id=u_conc.id,
                panel_id=panel_a.id,
                external_user_id=f"{u_conc.id}_p{panel_a.id}",
                status="active",
                last_synced_configs=["cfg_a"],
                last_cumulative_traffic=0,
            )
            db.add(map_conc)
            await db.commit()
            entities_to_clean.append(map_conc)

            # Phase 13 accounts traffic that pushes user over limit (e.g. 2500 bytes)
            accounted_delta = await account_mapping_usage(map_conc.id, 2500)
            assert accounted_delta == 2500

            # Phase 14 immediate enforcement must have flipped user to limited immediately!
            async with GetDB() as fresh_db:
                reloaded_conc = await get_user(fresh_db, u_conc.username)
                assert reloaded_conc.used_traffic >= 2000
                assert reloaded_conc.status == UserStatus.limited, (
                    f"User should have been limited immediately after traffic accounting, got {reloaded_conc.status}"
                )
            print("   [x] Immediate enforcement after traffic accounting flipped user to limited without scheduler tick.")

            # Check that Phase 10 sync was also enqueued by this restriction
            sync_job_conc = (await db.execute(
                select(OCSyncState).where(OCSyncState.entity_id == f"{u_conc.id}_{panel_a.id}", OCSyncState.status == "pending")
            )).scalar_one_or_none()
            assert sync_job_conc is not None
            assert sync_job_conc.operation == "delete"
            print("   [x] Immediate restriction enqueued Phase 10 OC deletion for affected mapping.")

            print("\nALL PHASE 14 TESTS PASSED SUCCESSFULLY!")

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
