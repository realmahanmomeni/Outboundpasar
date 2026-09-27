import asyncio
import sys
import uuid
import datetime
if not hasattr(datetime, 'UTC'):
    datetime.UTC = datetime.timezone.utc
from datetime import UTC, datetime as dt
from unittest.mock import AsyncMock, patch

import aiohttp
from sqlalchemy import select

from app.db import GetDB
from app.db.models import Admin, User
from app.db.models_oc import OCIntegration, OCPanel, OCUserMapping
from app.node.oc_usage import (
    account_mapping_usage,
    fetch_oc_user_usage,
    record_oc_user_usages,
)
from app.node.user import get_panel_xray_identity, parse_xray_identity
from app.jobs.record_usages import (
    _process_users_stats_response,
    calculate_users_usage,
)


class MockProtoStat:
    def __init__(self, name: str, value: int):
        self.name = name
        self.value = value


class MockStatsResponse:
    def __init__(self, stats: list[MockProtoStat]):
        self.stats = stats


async def run_tests():
    print("=== Running Phase 13 Traffic / Usage Accounting Tests ===")

    entities_to_clean = []

    try:
        # =========================================================================
        # 1. Identity Parsing Tests
        # =========================================================================
        print("--- 1. Testing Identity Parsing ---")

        # Native numeric identity
        assert parse_xray_identity("123") == (123, None)
        assert parse_xray_identity(123) == (123, None)
        assert parse_xray_identity("  456  ") == (456, None)

        # Panel-scoped identity
        assert parse_xray_identity("123_p92") == (123, 92)
        assert parse_xray_identity("999_p1") == (999, 1)
        assert get_panel_xray_identity(123, 92) == "123_p92"

        # Invalid identities
        assert parse_xray_identity("abc") is None
        assert parse_xray_identity("123p92") is None
        assert parse_xray_identity("_p92") is None
        assert parse_xray_identity("123_p") is None
        assert parse_xray_identity("") is None
        assert parse_xray_identity("invalid_user_string") is None

        # Stats response processor with mixed identities
        mock_response = MockStatsResponse([
            MockProtoStat("101", 1000),           # Native
            MockProtoStat("101_p10", 2000),       # Panel 10
            MockProtoStat("101_p20", 3000),       # Panel 20
            MockProtoStat("invalid_stat", 5000),  # Invalid
            MockProtoStat("abc_p99", 5000),       # Invalid
        ])
        validated, invalid = _process_users_stats_response(mock_response)
        assert len(invalid) == 2
        assert "invalid_stat" in invalid
        assert "abc_p99" in invalid
        assert len(validated) == 3

        # Check validated items
        v_native = next(v for v in validated if v["uid"] == 101 and v["panel_id"] is None)
        assert v_native["value"] == 1000

        v_p10 = next(v for v in validated if v["uid"] == 101 and v["panel_id"] == 10)
        assert v_p10["value"] == 2000

        v_p20 = next(v for v in validated if v["uid"] == 101 and v["panel_id"] == 20)
        assert v_p20["value"] == 3000

        # Calculate user usage aggregation with panel multiplier
        api_params = {1: validated}
        usage_coeff = {1: 1.0}
        panel_multipliers = {10: 1.5, 20: 2.0}
        # Expected for user 101: 1000 + (2000 * 1.5 = 3000) + (3000 * 2.0 = 6000) = 10,000
        aggregated = await calculate_users_usage(api_params, usage_coeff, panel_multipliers)
        assert len(aggregated) == 1
        assert aggregated[0]["uid"] == 101
        assert aggregated[0]["value"] == 10000

        print("   [x] parse_xray_identity and _process_users_stats_response work correctly for both native and panel identities.")

        # =========================================================================
        # Database Setup for Accounting Tests
        # =========================================================================
        async with GetDB() as db:
            admin = (await db.execute(select(Admin).limit(1))).scalar_one_or_none()
            if not admin:
                admin = Admin(username=f"admin_{uuid.uuid4().hex[:6]}", hashed_password="pw")
                db.add(admin)
                await db.flush()
                entities_to_clean.append(admin)
            admin_initial_traffic = admin.used_traffic or 0

            # Integration
            integration = OCIntegration(
                base_url="https://oc.test.local",
                api_token_encrypted="encrypted_tok",
                token_preview="tok",
                is_active=True,
            )
            db.add(integration)
            await db.flush()
            entities_to_clean.append(integration)

            # Panel 1 (multiplier = 1.0)
            panel1 = OCPanel(
                integration_id=integration.id,
                source_panel_id="panel_1",
                purchaser_identity="admin",
                name="Test Panel 1",
                multiplier=1.00,
            )
            # Panel 2 (multiplier = 2.5)
            panel2 = OCPanel(
                integration_id=integration.id,
                source_panel_id="panel_2",
                purchaser_identity="admin",
                name="Test Panel 2 (Multiplier)",
                multiplier=2.50,
            )
            # Panel 3 (multiplier = 1.0)
            panel3 = OCPanel(
                integration_id=integration.id,
                source_panel_id="panel_3",
                purchaser_identity="admin",
                name="Test Panel 3",
                multiplier=1.00,
            )
            db.add_all([panel1, panel2, panel3])
            await db.flush()
            entities_to_clean.extend([panel1, panel2, panel3])

            # User 1
            user1 = User(
                username=f"p13_u1_{uuid.uuid4().hex[:8]}",
                status="active",
                admin_id=admin.id,
                used_traffic=0,
            )
            db.add(user1)
            await db.flush()
            entities_to_clean.append(user1)

            # Mapping 1: User 1 on Panel 1
            map1 = OCUserMapping(
                user_id=user1.id,
                panel_id=panel1.id,
                external_user_id=f"{user1.id}_p{panel1.id}",
                last_cumulative_traffic=0,
                status="active",
            )
            db.add(map1)
            await db.flush()
            entities_to_clean.append(map1)

            await db.commit()

        # =========================================================================
        # 2. Basic Accounting Tests (Monotonic increase, Unchanged zero-delta, Delta)
        # =========================================================================
        print("--- 2. Testing Basic Accounting (First poll, Unchanged, Increased delta) ---")

        # Step 2a: First poll -> 10,000,000 bytes (10 MB)
        delta_applied = await account_mapping_usage(map1.id, 10_000_000)
        assert delta_applied == 10_000_000, f"Expected 10M delta, got {delta_applied}"

        async with GetDB() as db:
            m = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map1.id))).scalar_one()
            u = (await db.execute(select(User).where(User.id == user1.id))).scalar_one()
            assert m.last_cumulative_traffic == 10_000_000
            assert u.used_traffic == 10_000_000
            assert u.online_at is not None

        # Step 2b: Second poll -> Unchanged (10,000,000 bytes) -> Zero delta
        delta_applied = await account_mapping_usage(map1.id, 10_000_000)
        assert delta_applied == 0, f"Expected 0 delta for unchanged traffic, got {delta_applied}"

        async with GetDB() as db:
            m = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map1.id))).scalar_one()
            u = (await db.execute(select(User).where(User.id == user1.id))).scalar_one()
            assert m.last_cumulative_traffic == 10_000_000
            assert u.used_traffic == 10_000_000  # Still 10M

        # Step 2c: Third poll -> Increased (14,000,000 bytes) -> Delta of 4,000,000
        delta_applied = await account_mapping_usage(map1.id, 14_000_000)
        assert delta_applied == 4_000_000, f"Expected 4M delta, got {delta_applied}"

        async with GetDB() as db:
            m = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map1.id))).scalar_one()
            u = (await db.execute(select(User).where(User.id == user1.id))).scalar_one()
            assert m.last_cumulative_traffic == 14_000_000
            assert u.used_traffic == 14_000_000

        print("   [x] First poll (+10M), unchanged (0 delta), and increased (+4M) accounted accurately.")

        # =========================================================================
        # 3. Multi-Panel Isolation & User Cumulative Aggregation
        # =========================================================================
        print("--- 3. Testing Multi-Panel Isolation & Aggregation ---")

        # Attach User 1 to Panel 3 as well
        async with GetDB() as db:
            map3 = OCUserMapping(
                user_id=user1.id,
                panel_id=panel3.id,
                external_user_id=f"{user1.id}_p{panel3.id}",
                last_cumulative_traffic=0,
                status="active",
            )
            db.add(map3)
            await db.commit()
            entities_to_clean.append(map3)

        # Panel 1 reports 17 MB (delta = 17 - 14 = 3 MB)
        delta1 = await account_mapping_usage(map1.id, 17_000_000)
        assert delta1 == 3_000_000

        # Panel 3 reports 5 MB (delta = 5 - 0 = 5 MB)
        delta3 = await account_mapping_usage(map3.id, 5_000_000)
        assert delta3 == 5_000_000

        async with GetDB() as db:
            m1 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map1.id))).scalar_one()
            m3 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map3.id))).scalar_one()
            u = (await db.execute(select(User).where(User.id == user1.id))).scalar_one()
            # Panel 1 is isolated at 17 MB
            assert m1.last_cumulative_traffic == 17_000_000
            # Panel 3 is isolated at 5 MB
            assert m3.last_cumulative_traffic == 5_000_000
            # User total is 14 MB (previous) + 3 MB (Panel 1) + 5 MB (Panel 3) = 22 MB
            assert u.used_traffic == 22_000_000

        print("   [x] Multiple panels on same user maintain isolated counters and aggregate correctly.")

        # =========================================================================
        # 4. Multiplier Accounting Interaction
        # =========================================================================
        print("--- 4. Testing Multiplier Application ---")

        # Create Mapping on Panel 2 (multiplier = 2.50)
        async with GetDB() as db:
            map2 = OCUserMapping(
                user_id=user1.id,
                panel_id=panel2.id,
                external_user_id=f"{user1.id}_p{panel2.id}",
                last_cumulative_traffic=1_000_000,  # Baseline 1 MB
                status="active",
            )
            db.add(map2)
            await db.commit()
            entities_to_clean.append(map2)

        # Remote returns 3,000,000 bytes (raw delta = 2,000,000 bytes = 2 MB)
        # Multiplier is 2.50 -> accounted delta = 2,000,000 * 2.5 = 5,000,000 bytes (5 MB)
        delta2 = await account_mapping_usage(map2.id, 3_000_000)
        assert delta2 == 5_000_000, f"Expected 5M accounted delta, got {delta2}"

        async with GetDB() as db:
            m2 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map2.id))).scalar_one()
            u = (await db.execute(select(User).where(User.id == user1.id))).scalar_one()
            # Stored cumulative must be RAW remote bytes (3M), NOT multiplied bytes (7.5M)
            assert m2.last_cumulative_traffic == 3_000_000
            # User total increased by accounted 5 MB (22M + 5M = 27M)
            assert u.used_traffic == 27_000_000

        # Subsequent poll with same remote 3M yields zero delta
        delta2_repeat = await account_mapping_usage(map2.id, 3_000_000)
        assert delta2_repeat == 0

        print("   [x] Multiplier applies to delta only; raw remote baseline is preserved without multiplier drift.")

        # =========================================================================
        # 5. Remote Reset Handling (Decreased Cumulative Counter)
        # =========================================================================
        print("--- 5. Testing Remote Reset Handling ---")

        # Mapping 1 was at 17,000,000 bytes. Remote panel is reset to 0, user transfers 2,000,000 bytes since reset.
        # Remote API returns 2,000,000 (< 17,000,000).
        # Must NOT create negative delta (-15M). Must treat 2M as new traffic since reset.
        reset_delta = await account_mapping_usage(map1.id, 2_000_000)
        assert reset_delta == 2_000_000, f"Expected 2M delta after reset, got {reset_delta}"

        async with GetDB() as db:
            m1 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map1.id))).scalar_one()
            u = (await db.execute(select(User).where(User.id == user1.id))).scalar_one()
            # New baseline established at 2M
            assert m1.last_cumulative_traffic == 2_000_000
            # User traffic increased by +2M (27M + 2M = 29M), NEVER decreased!
            assert u.used_traffic == 29_000_000

        # Next normal tick from the new baseline (2M -> 3M)
        delta_next = await account_mapping_usage(map1.id, 3_000_000)
        assert delta_next == 1_000_000

        async with GetDB() as db:
            m1 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map1.id))).scalar_one()
            u = (await db.execute(select(User).where(User.id == user1.id))).scalar_one()
            assert m1.last_cumulative_traffic == 3_000_000
            assert u.used_traffic == 30_000_000

        print("   [x] Remote reset safely detected: no negative delta, new baseline established, continuous accounting.")

        # =========================================================================
        # 6. Concurrency Protection
        # =========================================================================
        print("--- 6. Testing Concurrency Protection (Row Locking) ---")

        # Simulate 5 concurrent workers attempting to account the exact same mapping at the same moment
        # Remote counter jumps from 3M to 5M (+2M delta).
        # Even with 5 parallel tasks, exactly 2M must be accounted in total, NOT 10M!
        concurrent_tasks = [
            account_mapping_usage(map1.id, 5_000_000)
            for _ in range(5)
        ]
        results = await asyncio.gather(*concurrent_tasks)
        total_added = sum(results)
        assert total_added == 2_000_000, f"Expected total 2M added across concurrent workers, got {total_added} (results: {results})"

        async with GetDB() as db:
            m1 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map1.id))).scalar_one()
            u = (await db.execute(select(User).where(User.id == user1.id))).scalar_one()
            assert m1.last_cumulative_traffic == 5_000_000
            assert u.used_traffic == 32_000_000  # 30M + 2M

        print("   [x] Concurrent worker collision safely prevented by row-level locking (exactly 1 worker accounts delta).")

        # =========================================================================
        # 7. Failure Isolation (One Panel Failure Does Not Abort Others)
        # =========================================================================
        print("--- 7. Testing Failure Isolation ---")

        mock_responses = {
            # Panel 1: Failure (502 / timeout)
            f"{integration.base_url}/v1/integration/panels/{panel1.source_panel_id}/users/{map1.external_user_id}/usage": None,
            # Panel 3: Success (6M -> delta = 6M - 5M = 1M)
            f"{integration.base_url}/v1/integration/panels/{panel3.source_panel_id}/users/{map3.external_user_id}/usage": 6_000_000,
        }

        async def mock_fetch_oc(base_url, token, source_panel_id, external_user_id, session=None):
            url = f"{base_url.rstrip('/')}/v1/integration/panels/{source_panel_id}/users/{external_user_id}/usage"
            if url in mock_responses:
                val = mock_responses[url]
                if val is None:
                    raise aiohttp.ClientError("Simulated connection timeout")
                return val
            return None

        with patch("app.node.oc_usage.fetch_oc_user_usage", new=mock_fetch_oc), \
             patch("app.node.oc_usage.decrypt_secret", new=AsyncMock(return_value="valid_tok")):
            # Run the overall batch collector
            total_collector_delta = await record_oc_user_usages()

        # Panel 1 failed and was skipped, Panel 3 succeeded and accounted 1M
        async with GetDB() as db:
            m1 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map1.id))).scalar_one()
            m3 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map3.id))).scalar_one()
            u = (await db.execute(select(User).where(User.id == user1.id))).scalar_one()
            assert m1.last_cumulative_traffic == 5_000_000  # Unchanged
            assert m3.last_cumulative_traffic == 6_000_000  # Updated
            assert u.used_traffic == 33_000_000            # 32M + 1M

        print("   [x] Panel 1 failure did not block Panel 3 accounting.")

        # =========================================================================
        # 8. Mapping Lifecycle (Deleted / Missing user behavior)
        # =========================================================================
        print("--- 8. Testing Mapping Lifecycle ---")

        # Mark map3 as deleted
        async with GetDB() as db:
            m3 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map3.id))).scalar_one()
            m3.status = "deleted"
            await db.commit()

        # Deleted mapping is skipped by account_mapping_usage and record_oc_user_usages
        delta_deleted = await account_mapping_usage(map3.id, 10_000_000)
        assert delta_deleted == 0

        async with GetDB() as db:
            m3 = (await db.execute(select(OCUserMapping).where(OCUserMapping.id == map3.id))).scalar_one()
            assert m3.last_cumulative_traffic == 6_000_000  # Not updated
            assert m3.status == "deleted"                   # Status preserved

        print("   [x] Deleted mappings are safely excluded from accounting.")

        # =========================================================================
        # 9. Admin Cumulative Usage Tracking
        # =========================================================================
        print("--- 9. Testing Admin Usage Accumulation ---")
        async with GetDB() as db:
            admin_after = (await db.execute(select(Admin).where(Admin.id == admin.id))).scalar_one()
            # User 1 had 33M traffic total in our tests
            admin_gained = admin_after.used_traffic - admin_initial_traffic
            assert admin_gained == 33_000_000, f"Expected admin to gain 33M, gained {admin_gained}"

        print("   [x] Admin used_traffic accurately reflects total user traffic accumulated.")

        # =========================================================================
        # 10. Scheduler Job Integration (_record_user_usages_impl)
        # =========================================================================
        print("--- 10. Testing Scheduler Job Integration ---")
        from app.jobs.record_usages import _record_user_usages_impl

        # Running the full scheduler job with mocked OC API
        with patch("app.node.oc_usage.fetch_oc_user_usage", new=AsyncMock(return_value=5_000_000)), \
             patch("app.node.oc_usage.decrypt_secret", new=AsyncMock(return_value="valid_tok")):
            # Should execute both node usage and OC usage cleanly
            await _record_user_usages_impl()

        print("   [x] _record_user_usages_impl runs successfully without errors.")

        print("\nALL PHASE 13 TRAFFIC ACCOUNTING TESTS PASSED SUCCESSFULLY!")

    finally:
        # Cleanup created test entities in reverse dependency order
        async with GetDB() as db:
            for entity in reversed(entities_to_clean):
                try:
                    await db.delete(entity)
                except Exception:
                    pass
            await db.commit()


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
