import asyncio
import uuid
import sys
from unittest.mock import AsyncMock, patch
import aiohttp
import datetime

if not hasattr(datetime, 'UTC'):
    datetime.UTC = datetime.timezone.utc
from datetime import UTC, datetime as dt

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.models import User, Group, ProxyInbound, Admin, UserStatus
from app.db.models_oc import OCUserMapping, OCSyncState, OCPanel, OCPanelConfig, OCIntegration
from app.node.oc_sync import enqueue_oc_user_sync
from app.jobs.process_oc_sync import process_oc_sync
from app.jobs.reconcile_oc_sync import reconcile_all_oc_users

async def setup_base_data(db):
    test_admin = (await db.execute(select(Admin).limit(1))).scalar_one_or_none()
    
    integration = OCIntegration(base_url="http://test", api_token_encrypted="encrypted_token", token_preview="test")
    db.add(integration)
    await db.flush()
    
    panel = OCPanel(integration_id=integration.id, source_panel_id="1", purchaser_identity="test", name="Test Panel")
    panel2 = OCPanel(integration_id=integration.id, source_panel_id="2", purchaser_identity="test", name="Test Panel 2")
    db.add_all([panel, panel2])
    await db.flush()
    
    return test_admin, integration, panel, panel2

async def cleanup_base_data(db, entities):
    for entity in entities:
        try:
            await db.delete(entity)
        except Exception:
            pass
    await db.commit()
    await db.execute(OCSyncState.__table__.delete())
    await db.commit()

class MockResponse:
    def __init__(self, status=200):
        self.status = status
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        pass
    def raise_for_status(self):
        if self.status >= 400:
            raise Exception("Mock HTTP Error")

def mock_put_success(*args, **kwargs):
    return MockResponse(200)

def mock_delete_success(*args, **kwargs):
    return MockResponse(200)
    
def mock_delete_404(*args, **kwargs):
    return MockResponse(404)

def mock_put_failure(*args, **kwargs):
    return MockResponse(500)

def mock_delete_failure(*args, **kwargs):
    return MockResponse(500)

async def mock_decrypt(*args, **kwargs):
    return "decrypted_token"

async def test_put_delete_success_failure():
    print("--- Running F, G, H, I: PUT/DELETE Success & Failure ---")
    async with GetDB() as db:
        admin, intg, p1, p2 = await setup_base_data(db)
        
        user = User(username=f"user_{uuid.uuid4().hex[:8]}", status="active", admin_id=admin.id)
        db.add(user)
        await db.flush()
        
        mapping = OCUserMapping(user_id=user.id, panel_id=p1.id, external_user_id=str(uuid.uuid4()), status="active", last_synced_configs=["config-a"])
        db.add(mapping)
        await db.commit()
        
        # Test H: PUT FAILURE
        job_put = OCSyncState(entity_type="user_mapping", entity_id=f"{user.id}_{p1.id}", operation="update", idempotency_key=f"sync_{uuid.uuid4()}", payload={"configs": ["config-a", "config-b"]})
        db.add(job_put)
        await db.commit()
        
        with patch("aiohttp.ClientSession.put", new=mock_put_failure), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
            await process_oc_sync()
            
        async with GetDB() as db2:
            m = (await db2.execute(select(OCUserMapping).where(OCUserMapping.id == mapping.id))).scalar_one()
            j = (await db2.execute(select(OCSyncState).where(OCSyncState.id == job_put.id))).scalar_one()
            assert m.last_synced_configs == ["config-a"] # Preserved
            assert j.attempts == 1
            assert j.status == "pending"
            print("   [x] H: PUT Failure preserved state.")
            
            # Run worker repeatedly until it reaches terminal failure state
            attempts = j.attempts
            max_attempts = j.max_attempts
            
        for _ in range(max_attempts - attempts):
            with patch("aiohttp.ClientSession.put", new=mock_put_failure), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
                await process_oc_sync()
            
        async with GetDB() as db3:
            j = (await db3.execute(select(OCSyncState).where(OCSyncState.id == job_put.id))).scalar_one()
            m = (await db3.execute(select(OCUserMapping).where(OCUserMapping.id == mapping.id))).scalar_one()
            assert j.status == "failed"
            assert j.attempts == j.max_attempts
            assert m.last_synced_configs == ["config-a"] # Not incorrectly updated
            print("   [x] Job reached terminal failed state.")
            
        # Test J: FAILED JOB RECOVERY
        # We need an inbound and group to generate desired state so reconcile enqueues something
        group = Group(name=f"Group_{uuid.uuid4().hex[:8]}", is_disabled=False, inbounds=[])
        inbound1 = ProxyInbound(tag=f"oc_{p1.id}_inb1_{uuid.uuid4().hex[:8]}")
        inbound2 = ProxyInbound(tag=f"oc_{p1.id}_inb2_{uuid.uuid4().hex[:8]}")
        group.inbounds = [inbound1, inbound2]
        
        # reload user with groups
        async with GetDB() as db_inner:
            user = (await db_inner.execute(select(User).options(selectinload(User.groups)).where(User.id == user.id))).scalar_one()
            user.groups.append(group)
            db_inner.add_all([group, inbound1, inbound2])
            await db_inner.flush()
            
            config1 = OCPanelConfig(panel_id=p1.id, source_config_id="config-a", source_name="A", virtual_inbound_tag=inbound1.tag)
            config2 = OCPanelConfig(panel_id=p1.id, source_config_id="config-b", source_name="B", virtual_inbound_tag=inbound2.tag)
            db_inner.add_all([config1, config2])
            await db_inner.commit()
        
        await reconcile_all_oc_users()
        
        async with GetDB() as db4:
            j = (await db4.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{p1.id}", OCSyncState.status == "pending"))).scalar_one()
            assert j.status == "pending"
            assert j.operation == "update"
            assert j.attempts == 0
            assert j.payload["configs"] == ["config-a", "config-b"]
            print("   [x] J: Failed Job Recovery successful.")

        # Test F: PUT SUCCESS
        with patch("aiohttp.ClientSession.put", new=mock_put_success), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
            await process_oc_sync()
            
        async with GetDB() as db5:
            m = (await db5.execute(select(OCUserMapping).where(OCUserMapping.id == mapping.id))).scalar_one()
            j = (await db5.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{p1.id}", OCSyncState.status == "completed"))).scalars().all()[-1]
            assert m.last_synced_configs == ["config-a", "config-b"]
            assert j.status == "completed"
            print("   [x] F: PUT Success updated state.")

        # Test I: DELETE FAILURE
        # First queue delete
        group.is_disabled = True
        await db.flush()
        await enqueue_oc_user_sync(db, user)
        await db.commit()
        
        with patch("aiohttp.ClientSession.delete", new=mock_delete_failure), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
            await process_oc_sync()
            
        async with GetDB() as db6:
            m = (await db6.execute(select(OCUserMapping).where(OCUserMapping.id == mapping.id))).scalar_one()
            j = (await db6.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{p1.id}", OCSyncState.status == "pending"))).scalar_one()
            assert m.last_synced_configs == ["config-a", "config-b"]
            assert m.status == "active"
            assert j.attempts == 1
            print("   [x] I: DELETE Failure preserved state.")
            
        # Test G: DELETE SUCCESS
        with patch("aiohttp.ClientSession.delete", new=mock_delete_success), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
            await process_oc_sync()
            
        async with GetDB() as db7:
            m = (await db7.execute(select(OCUserMapping).where(OCUserMapping.id == mapping.id))).scalar_one()
            j = (await db7.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{p1.id}", OCSyncState.operation == "delete"))).scalars().all()[-1]
            assert m.last_synced_configs == []
            assert m.status == "deleted"
            assert j.status == "completed"
            print("   [x] G: DELETE Success updated state.")
            
        await cleanup_base_data(db, [user, group, inbound1, inbound2, config1, config2, mapping, intg, p1, p2])

async def test_race_windows_and_isolation():
    print("--- Running L, M, O: Race Windows & Isolation ---")
    async with GetDB() as db:
        admin, intg, p1, p2 = await setup_base_data(db)
        
        user = User(username=f"user_{uuid.uuid4().hex[:8]}", status="active", admin_id=admin.id)
        db.add(user)
        await db.flush()
        
        m1 = OCUserMapping(user_id=user.id, panel_id=p1.id, external_user_id=str(uuid.uuid4()), status="active", last_synced_configs=["config-a"])
        m2 = OCUserMapping(user_id=user.id, panel_id=p2.id, external_user_id=str(uuid.uuid4()), status="active", last_synced_configs=["config-b"])
        db.add_all([m1, m2])
        await db.commit()
        
        job1 = OCSyncState(entity_type="user_mapping", entity_id=f"{user.id}_{p1.id}", operation="update", idempotency_key=f"sync_{uuid.uuid4()}", payload={"configs": ["config-a", "config-a2"]}, revision=1)
        db.add(job1)
        await db.commit()
        
        # Test M: RACE WINDOW 1 (HTTP blocked, enqueue happens)
        class MockPutRace1:
            async def __aenter__(self):
                print("     [Mock Race 1] HTTP blocked. Running enqueue...")
                # Simulate enqueue during HTTP
                async with GetDB() as db_inner:
                    j = (await db_inner.execute(select(OCSyncState).where(OCSyncState.id == job1.id))).scalar_one()
                    j.payload = {"configs": ["config-a", "config-a2", "config-a3"]}
                    j.revision += 1
                    await db_inner.commit()
                return MockResponse(200)
            async def __aexit__(self, exc_type, exc, tb):
                pass
        
        def mock_put_race1(*args, **kwargs):
            return MockPutRace1()
            
        with patch("aiohttp.ClientSession.put", new=mock_put_race1), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
            await process_oc_sync()
            
        async with GetDB() as db2:
            m1_check = (await db2.execute(select(OCUserMapping).where(OCUserMapping.id == m1.id))).scalar_one()
            j1_check = (await db2.execute(select(OCSyncState).where(OCSyncState.id == job1.id))).scalar_one()
            assert m1_check.last_synced_configs == ["config-a"] # Stale worker aborted
            assert j1_check.status == "pending"
            assert j1_check.revision == 2
            print("   [x] M (Race 1): Stale worker safely aborted.")
            
        # Test M: RACE WINDOW 2 (Worker after HTTP, right before CAS)
        # We need to monkeypatch db.execute in process_oc_sync to intercept the CAS update
        
        original_execute = None
        db_pool = None
        
        async def intercept_execute(self, stmt, *args, **kwargs):
            stmt_str = str(stmt).lower()
            if "update oc_sync_states" in stmt_str and "status" in stmt_str and "revision" in stmt_str:
                print("     [Mock Race 2] Intercepted CAS Update! Enqueueing...")
                async with GetDB() as db_inner:
                    j = (await db_inner.execute(select(OCSyncState).where(OCSyncState.id == job1.id))).scalar_one()
                    j.payload = {"configs": ["config-a", "config-a2", "config-a3", "config-a4"]}
                    j.revision += 1
                    await db_inner.commit()
                print("     [Mock Race 2] Enqueue committed. Resuming CAS Update...")
            return await original_execute(self, stmt, *args, **kwargs)

        async with GetDB() as db_hack:
            original_execute = db_hack.__class__.execute
            with patch.object(db_hack.__class__, 'execute', new=intercept_execute):
                with patch("aiohttp.ClientSession.put", new=mock_put_success), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
                    await process_oc_sync()
                    
        async with GetDB() as db3:
            m1_check = (await db3.execute(select(OCUserMapping).where(OCUserMapping.id == m1.id))).scalar_one()
            j1_check = (await db3.execute(select(OCSyncState).where(OCSyncState.id == job1.id))).scalar_one()
            assert m1_check.last_synced_configs == ["config-a"] # Stale worker aborted because CAS failed!
            assert j1_check.status == "pending"
            assert j1_check.revision == 3
            assert j1_check.payload["configs"] == ["config-a", "config-a2", "config-a3", "config-a4"]
            print("   [x] M (Race 2): CAS successfully blocked stale commit!")
            
        # Complete the final state (Test L: Latest state wins)
        with patch("aiohttp.ClientSession.put", new=mock_put_success), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
            await process_oc_sync()
            
        async with GetDB() as db4:
            m1_check = (await db4.execute(select(OCUserMapping).where(OCUserMapping.id == m1.id))).scalar_one()
            j1_check = (await db4.execute(select(OCSyncState).where(OCSyncState.id == job1.id))).scalar_one()
            assert m1_check.last_synced_configs == ["config-a", "config-a2", "config-a3", "config-a4"]
            assert j1_check.status == "completed"
            print("   [x] L: Latest state won and successfully applied.")
            
        # Test O: Multiple Panel Isolation
        async with GetDB() as db5:
            m2_check = (await db5.execute(select(OCUserMapping).where(OCUserMapping.id == m2.id))).scalar_one()
            assert m2_check.last_synced_configs == ["config-b"]
            print("   [x] O: Multiple Panel Isolation works (Panel 2 untouched).")
            
        await cleanup_base_data(db, [user, m1, m2, intg, p1, p2])

async def test_reconcile_and_discovery():
    print("--- Running P, Q, R: Reconcile & Discovery ---")
    async with GetDB() as db:
        admin, intg, p1, p2 = await setup_base_data(db)
        
        # Test R: New user+panel discovery
        user = User(username=f"user_{uuid.uuid4().hex[:8]}", status="active", admin_id=admin.id)
        group = Group(name=f"Group_{uuid.uuid4().hex[:8]}", is_disabled=False, inbounds=[])
        inbound1 = ProxyInbound(tag=f"oc_{p1.id}_inb1_{uuid.uuid4().hex[:8]}")
        group.inbounds = [inbound1]
        user.groups = [group]
        db.add_all([user, group, inbound1])
        await db.flush()
        
        config1 = OCPanelConfig(panel_id=p1.id, source_config_id="config-a", source_name="A", virtual_inbound_tag=inbound1.tag)
        db.add(config1)
        await db.commit()
        
        await reconcile_all_oc_users()
        
        async with GetDB() as db2:
            m = (await db2.execute(select(OCUserMapping).where(OCUserMapping.user_id == user.id))).scalar_one()
            j = (await db2.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{p1.id}"))).scalar_one()
            assert m.external_user_id is not None
            assert j.status == "pending"
            assert j.payload["configs"] == ["config-a"]
            print("   [x] R: New mapping successfully discovered and job created.")
            
        # Test P: Repeated reconciliation
        await reconcile_all_oc_users()
        async with GetDB() as db3:
            jobs = (await db3.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{p1.id}"))).scalars().all()
            assert len(jobs) == 1
            print("   [x] P: Repeated reconciliation is idempotent.")
            
        # Test Q: NULL vs []
        # m currently has NULL last_synced_configs, and desired is ["config-a"]. Job is created.
        # Now we process it so it becomes ["config-a"]
        with patch("aiohttp.ClientSession.put", new=mock_put_success), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
            await process_oc_sync()
            
        # Now disable the group so desired is [].
        group.is_disabled = True
        await db.commit()
        await reconcile_all_oc_users() # Creates delete job
        
        with patch("aiohttp.ClientSession.delete", new=mock_delete_success), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
            await process_oc_sync() # Processes delete job
            
        async with GetDB() as db4:
            m = (await db4.execute(select(OCUserMapping).where(OCUserMapping.user_id == user.id))).scalar_one()
            assert m.last_synced_configs == []
            assert m.status == "deleted"
            
        # Re-run reconciliation when last_synced_configs=[] and status=deleted and desired=[]
        await db.execute(OCSyncState.__table__.delete())
        await db.commit()
        await reconcile_all_oc_users()
        
        async with GetDB() as db5:
            jobs = (await db5.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{p1.id}"))).scalars().all()
            assert len(jobs) == 0
            print("   [x] Q: NULL vs [] correctly differentiated (no unnecessary deletes).")
            
        await cleanup_base_data(db, [user, group, inbound1, config1, intg, p1, p2])

async def run_tests():
    await test_put_delete_success_failure()
    await test_race_windows_and_isolation()
    await test_reconcile_and_discovery()
    print("ALL TESTS PASSED!")

if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
