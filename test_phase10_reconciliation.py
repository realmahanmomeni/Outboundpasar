import asyncio
import uuid
import sys
import datetime
if not hasattr(datetime, 'UTC'):
    datetime.UTC = datetime.timezone.utc
from datetime import UTC, datetime as dt
from sqlalchemy import select

from app.db import GetDB
from app.db.models import User, Group, ProxyInbound, Admin
from app.db.models_oc import OCUserMapping, OCSyncState, OCPanel, OCPanelConfig, OCIntegration
from app.node.oc_sync import enqueue_oc_user_sync

async def run_tests():
    print("Running Phase 10 Tests...")
    
    async with GetDB() as db:
        test_admin = (await db.execute(select(Admin).limit(1))).scalar_one_or_none()
        if not test_admin:
            print("No admin found, skipping tests")
            return
            
    entities_to_delete = []
    
    try:
        async with GetDB() as db:
            # Setup base data
            integration = OCIntegration(base_url="http://test", api_token_encrypted="encrypted_token", token_preview="test")
            db.add(integration)
            await db.flush()
            
            panel = OCPanel(
                integration_id=integration.id,
                source_panel_id="1",
                purchaser_identity="test",
                name="Test Panel",
                multiplier=1.0
            )
            db.add(panel)
            await db.flush()
            entities_to_delete.extend([integration, panel])
            
            user = User(username=f"test_phase10_{uuid.uuid4().hex[:8]}", status="active", admin_id=test_admin.id)
            group = Group(name=f"Group_{uuid.uuid4().hex[:8]}", is_disabled=False, inbounds=[])
            
            inbound1 = ProxyInbound(tag=f"oc_{panel.id}_inbound1_{uuid.uuid4().hex[:8]}")
            inbound2 = ProxyInbound(tag=f"oc_{panel.id}_inbound2_{uuid.uuid4().hex[:8]}")
            
            group.inbounds = [inbound1, inbound2]
            user.groups = [group]
            
            db.add_all([user, group, inbound1, inbound2])
            await db.flush()
            
            config1 = OCPanelConfig(
                panel_id=panel.id,
                source_config_id="config-a",
                source_name="Config A",
                virtual_inbound_tag=inbound1.tag
            )
            config2 = OCPanelConfig(
                panel_id=panel.id,
                source_config_id="config-b",
                source_name="Config B",
                virtual_inbound_tag=inbound2.tag
            )
            db.add_all([config1, config2])
            await db.flush()
            
            entities_to_delete.extend([user, group, inbound1, inbound2, config1, config2])
            
            # --- TEST 1: NO-OP ---
            mapping = OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id=str(uuid.uuid4()),
                status="active",
                last_synced_configs=["config-a", "config-b"]
            )
            db.add(mapping)
            await db.commit()
            
            await enqueue_oc_user_sync(db, user)
            await db.flush()
            jobs = (await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalars().all()
            assert len(jobs) == 0, "Expected NO-OP but got jobs"
            print("   [x] test_reconciliation_noop OK")
            
            # --- TEST 2: Config removed ---
            mapping.last_synced_configs = ["config-a", "config-b", "config-c"]
            await db.commit()
            await enqueue_oc_user_sync(db, user)
            await db.flush()
            jobs = (await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalars().all()
            print("TEST 2 jobs:", jobs)
            if jobs:
                print("job 0 payload:", jobs[0].payload)
            print("TEST 2 mapping last configs:", mapping.last_synced_configs)
            assert len(jobs) == 1, f"Expected 1 update job, got {len(jobs)}"
            assert jobs[0].operation == "update"
            assert jobs[0].payload["configs"] == ["config-a", "config-b"]
            print("   [x] test_reconciliation_config_removed OK")
            
            # --- TEST 3: Config added ---
            await db.execute(OCSyncState.__table__.delete().where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))
            
            mapping.last_synced_configs = ["config-a"]
            await db.commit()
            await enqueue_oc_user_sync(db, user)
            await db.flush()
            jobs = (await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalars().all()
            assert len(jobs) == 1, "Expected 1 update job"
            assert jobs[0].operation == "update"
            assert jobs[0].payload["configs"] == ["config-a", "config-b"]
            print("   [x] test_reconciliation_config_added OK")
            
            # --- TEST 4: Idempotent Queue / Latest State Wins ---
            # Remove the previous job
            await db.execute(OCSyncState.__table__.delete().where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))
            await db.commit()
            
            # Simulate rapid changes
            group.inbounds.remove(inbound2)
            await db.flush()
            await enqueue_oc_user_sync(db, user)
            await db.flush()
            
            group.inbounds.append(inbound2)
            await db.flush()
            await enqueue_oc_user_sync(db, user)
            await db.flush()
            
            jobs = (await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalars().all()
            assert len(jobs) == 1, f"Expected 1 job after multiple enqueues, got {len(jobs)}"
            assert jobs[0].payload["configs"] == ["config-a", "config-b"], "Expected latest state to win"
            print("   [x] test_idempotent_queue and test_latest_desired_state_wins OK")
            
            # --- TEST 5: Full removal ---
            await db.execute(OCSyncState.__table__.delete().where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))
            group.is_disabled = True
            await db.flush()
            
            await enqueue_oc_user_sync(db, user)
            await db.flush()
            jobs = (await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalars().all()
            assert len(jobs) == 1
            assert jobs[0].operation == "delete"
            assert jobs[0].payload["external_user_id"] == mapping.external_user_id
            print("   [x] test_reconciliation_full_removal OK")
            
            # --- TEST 6: Restore access ---
            await db.execute(OCSyncState.__table__.delete().where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))
            mapping.status = "deleted"
            mapping.last_synced_configs = []
            group.is_disabled = False
            await db.commit()
            
            await enqueue_oc_user_sync(db, user)
            await db.flush()
            jobs = (await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalars().all()
            assert len(jobs) == 1
            assert jobs[0].operation == "update"
            assert jobs[0].payload["configs"] == ["config-a", "config-b"]
            print("   [x] test_reconciliation_restore_access OK")
            
            print("\nALL PHASE 10 RECONCILIATION TESTS PASSED SUCCESSFULLY!")
            
    finally:
        async with GetDB() as db:
            for entity in entities_to_delete:
                try:
                    await db.delete(entity)
                except Exception:
                    pass
            await db.commit()

if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
