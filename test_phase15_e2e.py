import asyncio
import datetime
if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc
from datetime import UTC, datetime as dt, timedelta as td
import sys
import uuid
from unittest.mock import AsyncMock, patch

import aiohttp
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.crud.user import (
    create_user,
    get_user,
    reset_user_data_usage,
)
from app.db.models import (
    Admin,
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
from app.jobs.process_oc_sync import process_oc_sync
from app.node.oc_sync import enqueue_oc_user_sync
from app.node.oc_usage import (
    account_mapping_usage,
    fetch_oc_user_usage,
    record_oc_user_usages,
)
from app.node.user import serialize_user
from app.operation import OperatorType
from app.operation.access_control import (
    check_user_access_allowed,
    enforce_user_limits_now,
)
from app.operation.subscription import SubscriptionOperation
from app.subscription.share import generate_subscription


class MockResponse:
    def __init__(self, status=200, json_data=None):
        self.status = status
        self._json_data = json_data or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status >= 400:
            raise Exception(f"Mock HTTP Error {self.status}")


async def run_tests():
    print("=== Running Phase 15 End-to-End & Failure-Injection Tests ===")
    sub_op = SubscriptionOperation(operator_type=OperatorType.API)
    entities_to_clean = []

    try:
        async with GetDB() as db:
            admin = (await db.execute(select(Admin).limit(1))).scalar_one_or_none()
            assert admin is not None, "Admin record required"

            # =================================================================
            # 1. MULTI-PANEL ISOLATION TEST (Section 10)
            # =================================================================
            print("\n--- 1. Testing Multi-Panel Isolation (Panel 92 & Panel 93) ---")

            intg = OCIntegration(
                base_url="http://test-multi-panel",
                api_token_encrypted="encrypted_token",
                token_preview="multi",
                is_active=True,
            )
            db.add(intg)
            await db.flush()
            entities_to_clean.append(intg)

            # Panel 92 (e.g. Multiplier 1.5x)
            panel_92 = OCPanel(
                integration_id=intg.id,
                source_panel_id="panel_92",
                purchaser_identity="admin",
                name="Panel 92",
                multiplier=1.50,
            )
            # Panel 93 (e.g. Multiplier 2.5x)
            panel_93 = OCPanel(
                integration_id=intg.id,
                source_panel_id="panel_93",
                purchaser_identity="admin",
                name="Panel 93",
                multiplier=2.50,
            )
            db.add_all([panel_92, panel_93])
            await db.flush()
            entities_to_clean.extend([panel_92, panel_93])

            # Inbounds and configs for Panel 92
            tag_92 = f"oc_{panel_92.id}_c92"
            inb_92 = ProxyInbound(tag=tag_92)
            db.add(inb_92)
            await db.flush()
            entities_to_clean.append(inb_92)

            host_92 = ProxyHost(
                remark="Panel 92 Host",
                priority=1,
                address={"10.92.0.1"},
                status=[],
                alpn=[],
                port=443,
                path="/p92",
                allowinsecure=False,
                is_disabled=False,
            )
            host_92.inbound = inb_92
            db.add(host_92)
            await db.flush()
            entities_to_clean.append(host_92)

            cfg_92 = OCPanelConfig(
                panel_id=panel_92.id,
                source_config_id="c92",
                source_name="Config 92",
                virtual_inbound_tag=tag_92,
                source_missing=False,
                protocol="vless",
                network="tcp",
                port=443,
            )
            db.add(cfg_92)
            entities_to_clean.append(cfg_92)

            # Inbounds and configs for Panel 93
            tag_93 = f"oc_{panel_93.id}_c93"
            inb_93 = ProxyInbound(tag=tag_93)
            db.add(inb_93)
            await db.flush()
            entities_to_clean.append(inb_93)

            host_93 = ProxyHost(
                remark="Panel 93 Host",
                priority=1,
                address={"10.93.0.1"},
                status=[],
                alpn=[],
                port=443,
                path="/p93",
                allowinsecure=False,
                is_disabled=False,
            )
            host_93.inbound = inb_93
            db.add(host_93)
            await db.flush()
            entities_to_clean.append(host_93)

            cfg_93 = OCPanelConfig(
                panel_id=panel_93.id,
                source_config_id="c93",
                source_name="Config 93",
                virtual_inbound_tag=tag_93,
                source_missing=False,
                protocol="vless",
                network="tcp",
                port=443,
            )
            db.add(cfg_93)
            entities_to_clean.append(cfg_93)

            grp_both = Group(name=f"GrpBoth_{uuid.uuid4().hex[:6]}", inbounds=[inb_92, inb_93])
            db.add(grp_both)
            await db.flush()
            entities_to_clean.append(grp_both)

            # User 123
            user_123 = User(
                username=f"user123_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=50_000_000,
                used_traffic=0,
                admin_id=admin.id,
                proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            )
            user_123.groups = [grp_both]
            db.add(user_123)
            await db.flush()
            entities_to_clean.append(user_123)

            # User 123 has separate OCUserMappings on both panels
            map_92 = OCUserMapping(
                user_id=user_123.id,
                panel_id=panel_92.id,
                external_user_id=f"ext_123_p92_{uuid.uuid4().hex[:6]}",
                status="active",
                last_cumulative_traffic=0,
            )
            map_93 = OCUserMapping(
                user_id=user_123.id,
                panel_id=panel_93.id,
                external_user_id=f"ext_123_p93_{uuid.uuid4().hex[:6]}",
                status="active",
                last_cumulative_traffic=0,
            )
            db.add_all([map_92, map_93])
            await db.commit()
            entities_to_clean.extend([map_92, map_93])

            assert map_92.external_user_id != map_93.external_user_id
            print("   [x] Distinct external_user_ids verified across Panel 92 and Panel 93.")

            # 1.1 Sync failure isolation: Panel 92 succeeds, Panel 93 fails
            job_92 = OCSyncState(
                entity_type="user_mapping",
                entity_id=f"{user_123.id}_{panel_92.id}",
                operation="update",
                idempotency_key=f"sync_92_{uuid.uuid4()}",
                payload={"configs": [cfg_92.source_config_id]},
                status="pending",
                revision=1,
            )
            job_93 = OCSyncState(
                entity_type="user_mapping",
                entity_id=f"{user_123.id}_{panel_93.id}",
                operation="update",
                idempotency_key=f"sync_93_{uuid.uuid4()}",
                payload={"configs": [cfg_93.source_config_id]},
                status="pending",
                revision=1,
            )
            db.add_all([job_92, job_93])
            await db.commit()
            entities_to_clean.extend([job_92, job_93])

            def mock_put_selective(url, headers=None, json=None, **kwargs):
                if "panel_92" in url:
                    return MockResponse(200)
                elif "panel_93" in url:
                    return MockResponse(500)
                return MockResponse(404)

            with patch("aiohttp.ClientSession.put", side_effect=mock_put_selective), \
                 patch("app.jobs.process_oc_sync.decrypt_secret", new=AsyncMock(return_value="mock_token")):
                await process_oc_sync()

            await db.refresh(job_92)
            await db.refresh(job_93)
            await db.refresh(map_92)
            await db.refresh(map_93)

            assert job_92.status == "completed"
            assert map_92.last_synced_configs == [cfg_92.source_config_id]
            assert job_93.status == "pending"
            assert job_93.attempts == 1
            print("   [x] Panel 92 succeeded and Panel 93 failed in complete isolation.")

            # 1.2 Multiplier isolation: Panel 92 usage accounts with 1.5x, Panel 93 with 2.5x
            delta_92 = await account_mapping_usage(map_92.id, remote_cumulative=10_000)
            assert delta_92 == int(10_000 * 1.50)  # 15000

            delta_93 = await account_mapping_usage(map_93.id, remote_cumulative=20_000)
            assert delta_93 == int(20_000 * 2.50)  # 50000

            await db.refresh(user_123)
            assert user_123.used_traffic == 65_000
            print("   [x] Distinct panel multipliers (1.5x and 2.5x) accounted independently without collision.")

            # 1.3 Baseline reset isolation: Panel 92 resets, Panel 93 unaffected
            # Remote reset on 92 from 10,000 -> 2,000
            delta_reset_92 = await account_mapping_usage(map_92.id, remote_cumulative=2_000)
            assert delta_reset_92 == int(2_000 * 1.50)  # 3000

            # Normal progression on 93 from 20,000 -> 30,000
            delta_cont_93 = await account_mapping_usage(map_93.id, remote_cumulative=30_000)
            assert delta_cont_93 == int(10_000 * 2.50)  # 25000

            await db.refresh(map_92)
            await db.refresh(map_93)
            assert map_92.last_cumulative_traffic == 2_000
            assert map_93.last_cumulative_traffic == 30_000
            print("   [x] Panel 92 remote reset safely handled while Panel 93 baseline continued normally.")

            # =================================================================
            # 2. FULL END-TO-END USER LIFECYCLE (Section 11)
            # =================================================================
            print("\n--- 2. Testing Complete End-to-End User Lifecycle ---")
            # Step 1: New User
            u_life = User(
                username=f"life_{uuid.uuid4().hex[:8]}",
                status=UserStatus.active,
                data_limit=100_000,
                used_traffic=0,
                admin_id=admin.id,
                proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            )
            # Step 2: Panel Assignment
            u_life.groups = [grp_both]
            db.add(u_life)
            await db.flush()
            entities_to_clean.append(u_life)

            # Step 3 & 4: External User Creation
            m_life = OCUserMapping(
                user_id=u_life.id,
                panel_id=panel_92.id,
                external_user_id=f"ext_life_{uuid.uuid4().hex[:6]}",
                status="active",
                last_cumulative_traffic=0,
            )
            db.add(m_life)
            await db.commit()
            entities_to_clean.append(m_life)

            stable_sub_token = u_life.sub_token
            stable_ext_id = m_life.external_user_id

            # Step 5: Subscription Generation (Active)
            val_life = await sub_op.validated_user(u_life)
            assert len(val_life.inbounds) > 0
            sub_str = await generate_subscription(val_life, "links", False)
            assert "10.92.0.1" in sub_str
            print("   [x] Lifecycle: Active user subscription successfully generated.")

            # Step 6: Traffic Accounting
            accounted_acc = await account_mapping_usage(m_life.id, remote_cumulative=20_000)
            assert accounted_acc == int(20_000 * 1.50)
            await db.refresh(u_life)
            assert u_life.used_traffic == 30_000
            print("   [x] Lifecycle: Traffic accounted accurately.")

            # Step 7 & 8: Traffic Limit Reached -> Access Restriction
            accounted_limit = await account_mapping_usage(m_life.id, remote_cumulative=70_000)
            await db.refresh(u_life)
            assert u_life.used_traffic >= 100_000
            assert u_life.status == UserStatus.limited
            print("   [x] Lifecycle: Limit reached and access automatically restricted.")

            # Step 9: Verify Subscription and Native Node Drop Access
            proto_lim = await serialize_user(u_life)
            assert proto_lim[0].inbounds == [], "Limited user must have empty inbounds in node proto"

            val_life_lim = await sub_op.validated_user(u_life)
            assert val_life_lim.inbounds == [], "Limited user must have empty inbounds in subscription"
            sub_lim_str = await generate_subscription(val_life_lim, "links", False)
            assert sub_lim_str.strip() == "", "Limited user subscription must render empty config"
            assert u_life.sub_token == stable_sub_token, "Subscription token must remain unchanged"
            print("   [x] Lifecycle: Inbounds dropped across subscription and nodes while token remains invariant.")

            # Step 10: OC DELETE reconciliation job generated
            sync_del = (await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id == f"{u_life.id}_{panel_92.id}",
                    OCSyncState.operation == "delete",
                )
            )).scalar_one_or_none()
            assert sync_del is not None
            entities_to_clean.append(sync_del)

            # Step 11: Execute OC DELETE reconciliation
            with patch("aiohttp.ClientSession.delete", return_value=MockResponse(200)), \
                 patch("app.jobs.process_oc_sync.decrypt_secret", new=AsyncMock(return_value="mock_tok")):
                await process_oc_sync()

            await db.refresh(sync_del)
            assert sync_del.status == "completed"
            await db.refresh(m_life)
            assert m_life.status == "deleted"
            assert m_life.external_user_id == stable_ext_id, "external_user_id must remain preserved"
            print("   [x] Lifecycle: OC DELETE reconciliation executed; remote mapping preserved.")

            # Step 12: Traffic Reset / Renewal -> Access Restoration
            await reset_user_data_usage(db, u_life)
            await db.refresh(u_life)
            assert u_life.status == UserStatus.active
            assert u_life.used_traffic == 0

            # Step 13: OC PUT reconciliation enqueued
            await enqueue_oc_user_sync(db, u_life)
            await db.commit()

            sync_put = (await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id == f"{u_life.id}_{panel_92.id}",
                    OCSyncState.operation == "update",
                    OCSyncState.status == "pending",
                )
            )).scalar_one_or_none()
            assert sync_put is not None
            entities_to_clean.append(sync_put)

            with patch("aiohttp.ClientSession.put", return_value=MockResponse(200)), \
                 patch("app.jobs.process_oc_sync.decrypt_secret", new=AsyncMock(return_value="mock_tok")):
                await process_oc_sync()

            await db.refresh(sync_put)
            assert sync_put.status == "completed"
            await db.refresh(m_life)
            assert m_life.status == "active"
            assert m_life.external_user_id == stable_ext_id
            print("   [x] Lifecycle: OC PUT reconciliation executed; remote user restored.")

            # Step 14: Subscription remains stable and delivers configs again
            val_life_rest = await sub_op.validated_user(u_life)
            assert len(val_life_rest.inbounds) > 0
            sub_rest_str = await generate_subscription(val_life_rest, "links", False)
            assert "10.92.0.1" in sub_rest_str
            assert u_life.sub_token == stable_sub_token, "Subscription token must remain completely invariant"
            assert m_life.external_user_id == stable_ext_id, "external_user_id must remain completely invariant"
            print("   [x] Lifecycle: Subscription restored with invariant sub_token and external_user_id.")

            # =================================================================
            # 3. FAILURE-INJECTION TESTING (Section 12)
            # =================================================================
            print("\n--- 3. Testing Failure Injections & Boundary Robustness ---")

            # 3.1 Outbound Center Connection Timeout
            with patch("aiohttp.ClientSession.get", side_effect=TimeoutError()):
                timeout_res = await fetch_oc_user_usage("http://timeout", "tok", "p1", "u1")
                assert timeout_res is None
            print("   [x] Failure Injection: Connection timeout handled gracefully without crash.")

            # 3.2 Outbound Center HTTP 500
            with patch("aiohttp.ClientSession.get", return_value=MockResponse(500)):
                err_res = await fetch_oc_user_usage("http://err500", "tok", "p1", "u1")
                assert err_res is None
            print("   [x] Failure Injection: HTTP 500 handled gracefully without state mutation.")

            # 3.3 Malformed Upstream JSON
            with patch("aiohttp.ClientSession.get") as mock_bad_json:
                bad_resp = AsyncMock()
                bad_resp.status = 200
                bad_resp.json = AsyncMock(return_value={"upload_bytes": "corrupt_string_value"})
                mock_bad_json.return_value.__aenter__.return_value = bad_resp

                bad_json_res = await fetch_oc_user_usage("http://badjson", "tok", "p1", "u1")
                assert bad_json_res is None
            print("   [x] Failure Injection: Malformed JSON handled safely without exception.")

            # 3.4 Missing Upstream User on DELETE (HTTP 404 is graceful success)
            job_del_404 = OCSyncState(
                entity_type="user_mapping",
                entity_id=f"{u_life.id}_{panel_92.id}",
                operation="delete",
                idempotency_key=f"del_404_{uuid.uuid4()}",
                payload={"external_user_id": stable_ext_id},
                status="pending",
                revision=1,
            )
            db.add(job_del_404)
            await db.commit()
            entities_to_clean.append(job_del_404)

            with patch("aiohttp.ClientSession.delete", return_value=MockResponse(404)), \
                 patch("app.jobs.process_oc_sync.decrypt_secret", new=AsyncMock(return_value="tok")):
                await process_oc_sync()

            await db.refresh(job_del_404)
            assert job_del_404.status == "completed", "DELETE 404 should be treated as already deleted (completed)"
            print("   [x] Failure Injection: Missing remote user (404 on DELETE) completes cleanly.")

            # 3.5 Stale Worker Race (CAS revision mismatch)
            job_cas = OCSyncState(
                entity_type="user_mapping",
                entity_id=f"{u_life.id}_{panel_92.id}",
                operation="update",
                idempotency_key=f"cas_{uuid.uuid4()}",
                payload={"configs": ["cfg_old"]},
                status="pending",
                revision=1,
            )
            db.add(job_cas)
            await db.commit()
            entities_to_clean.append(job_cas)

            # Simulate concurrent worker bumping revision while HTTP request was inflight
            class SlowPutCM:
                def __init__(self, job_id):
                    self.job_id = job_id
                    self.status = 200

                async def __aenter__(self):
                    async with GetDB() as race_db:
                        await race_db.execute(
                            update(OCSyncState)
                            .where(OCSyncState.id == self.job_id)
                            .values(revision=2, payload={"configs": ["cfg_new"]})
                        )
                        await race_db.commit()
                    return self

                async def __aexit__(self, *args):
                    pass

                def raise_for_status(self):
                    pass

            def mock_slow_put(*args, **kwargs):
                return SlowPutCM(job_cas.id)

            with patch("aiohttp.ClientSession.put", side_effect=mock_slow_put), \
                 patch("app.jobs.process_oc_sync.decrypt_secret", new=AsyncMock(return_value="tok")):
                await process_oc_sync()

            await db.refresh(job_cas)
            assert job_cas.revision == 2
            assert job_cas.status == "pending", "Stale worker CAS failed: job was discarded and remains pending for newest revision"
            print("   [x] Failure Injection: Stale worker safely aborted via CAS revision check.")

            # 3.6 Concurrent accounting row-level locking
            concurrent_results = await asyncio.gather(
                account_mapping_usage(map_92.id, remote_cumulative=10_000),
                account_mapping_usage(map_92.id, remote_cumulative=10_000),
                return_exceptions=True,
            )
            valid_deltas = [r for r in concurrent_results if isinstance(r, int) and r > 0]
            assert len(valid_deltas) == 1, f"Row locking must ensure exactly 1 worker accounts delta, got {valid_deltas}"
            print("   [x] Concurrency: Row locking strictly prevents concurrent double-accounting.")

            print("\nALL PHASE 15 END-TO-END & FAILURE-INJECTION TESTS PASSED SUCCESSFULLY!")

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
