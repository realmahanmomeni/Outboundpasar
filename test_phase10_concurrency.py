import asyncio
import uuid
import sys
from unittest.mock import AsyncMock, patch
import aiohttp

import datetime
if not hasattr(datetime, 'UTC'):
    datetime.UTC = datetime.timezone.utc
from datetime import UTC, datetime as dt

from sqlalchemy import select
from app.db import GetDB
from app.db.models import User, Group, ProxyInbound, Admin
from app.db.models_oc import OCUserMapping, OCSyncState, OCPanel, OCPanelConfig, OCIntegration
from app.node.oc_sync import enqueue_oc_user_sync
from app.jobs.process_oc_sync import process_oc_sync
from app.jobs.reconcile_oc_sync import reconcile_all_oc_users

async def setup_test_data(db):
    test_admin = (await db.execute(select(Admin).limit(1))).scalar_one_or_none()
    
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
    
    user = User(username=f"test_phase10_{uuid.uuid4().hex[:8]}", status="active", admin_id=test_admin.id)
    group = Group(name=f"Group_{uuid.uuid4().hex[:8]}", is_disabled=False, inbounds=[])
    
    inbound1 = ProxyInbound(tag=f"oc_{panel.id}_inbound1_{uuid.uuid4().hex[:8]}")
    inbound2 = ProxyInbound(tag=f"oc_{panel.id}_inbound2_{uuid.uuid4().hex[:8]}")
    inbound3 = ProxyInbound(tag=f"oc_{panel.id}_inbound3_{uuid.uuid4().hex[:8]}")
    
    group.inbounds = [inbound1]
    user.groups = [group]
    db.add_all([user, group, inbound1, inbound2, inbound3])
    await db.flush()
    
    config1 = OCPanelConfig(panel_id=panel.id, source_config_id="config-a", source_name="A", virtual_inbound_tag=inbound1.tag)
    config2 = OCPanelConfig(panel_id=panel.id, source_config_id="config-b", source_name="B", virtual_inbound_tag=inbound2.tag)
    config3 = OCPanelConfig(panel_id=panel.id, source_config_id="config-c", source_name="C", virtual_inbound_tag=inbound3.tag)
    db.add_all([config1, config2, config3])
    await db.flush()
    
    mapping = OCUserMapping(
        user_id=user.id,
        panel_id=panel.id,
        external_user_id=str(uuid.uuid4()),
        status="active",
        last_synced_configs=["config-a"]
    )
    db.add(mapping)
    await db.commit()
    
    return integration, panel, user, group, inbound1, inbound2, inbound3, config1, config2, config3, mapping

async def run_tests():
    print("Running Phase 10 Worker & Concurrency Tests...")
    async with GetDB() as db:
        test_data = await setup_test_data(db)
        integration, panel, user, group, inbound1, inbound2, inbound3, config1, config2, config3, mapping = test_data

        try:
            # PART 11: REQUIRED CONCURRENCY TEST
            print("--- Running Concurrency Test (Part 11) ---")
            # 1. Initial state is ["config-a"]
            assert mapping.last_synced_configs == ["config-a"]

            # 2. Queue desired ["config-a", "config-b"]
            group.inbounds = [inbound1, inbound2]
            await db.commit()
            
            async with GetDB() as db2:
                user_fresh = (await db2.execute(select(User).where(User.id == user.id))).scalar_one()
                await enqueue_oc_user_sync(db2, user_fresh)
                await db2.commit()
            
            # Verify job is queued with A, B
            async with GetDB() as db3:
                job = (await db3.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalar_one()
                assert job.payload["configs"] == ["config-a", "config-b"]
            
            # 3. Start worker processing, pause HTTP request
            class MockResponse:
                def __init__(self):
                    self.status = 200
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc, tb):
                    pass
                def raise_for_status(self):
                    pass

            original_put = aiohttp.ClientSession.put
            
            class MockPutContextManager:
                async def __aenter__(self):
                    print("   [Worker] HTTP put is sleeping... Simulating concurrent update!")
                    try:
                        async with GetDB() as db4:
                            from sqlalchemy.orm import selectinload
                            user_fresh2 = (await db4.execute(select(User).where(User.id == user.id))).scalar_one()
                            group_fresh2 = (await db4.execute(select(Group).options(selectinload(Group.inbounds)).where(Group.id == group.id))).scalar_one()
                            inbound3_fresh2 = (await db4.execute(select(ProxyInbound).where(ProxyInbound.id == inbound3.id))).scalar_one()
                            group_fresh2.inbounds.append(inbound3_fresh2)
                            await db4.commit()
                            
                            from sqlalchemy.orm import selectinload
                            user_reloaded = (await db4.execute(
                                select(User).options(selectinload(User.groups).selectinload(Group.inbounds)).where(User.id == user.id)
                            )).scalar_one()
                            await enqueue_oc_user_sync(db4, user_reloaded)
                            await db4.commit()
                        
                        async with GetDB() as db5:
                            job_check = (await db5.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalar_one()
                            assert job_check.payload["configs"] == ["config-a", "config-b", "config-c"]
                        
                        print("   [Worker] Concurrent update finished. Returning success.")
                        return MockResponse()
                    except Exception as e:
                        print("ERROR IN MOCK PUT:", e)
                        raise e
                async def __aexit__(self, exc_type, exc, tb):
                    pass

            def mock_put(*args, **kwargs):
                return MockPutContextManager()

            async def mock_decrypt(*args, **kwargs):
                return "decrypted_token"

            with patch("aiohttp.ClientSession.put", new=mock_put), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
                # 4. Process
                await process_oc_sync()
            
            # 7. Assert stale worker does NOT leave ["config-a", "config-b"]
            async with GetDB() as db6:
                mapping_check = (await db6.execute(select(OCUserMapping).where(OCUserMapping.id == mapping.id))).scalar_one()
                # Should STILL be ["config-a"] because the worker aborted!
                assert mapping_check.last_synced_configs == ["config-a"], f"Expected 'config-a', got {mapping_check.last_synced_configs}"
                
                # 8. Assert latest desired state remains pending
                job_check = (await db6.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalar_one()
                assert job_check.status == "pending"
                assert job_check.payload["configs"] == ["config-a", "config-b", "config-c"]
            
            print("   [x] Concurrency test passed: Stale worker safely aborted!")

            # 9. Process the latest state successfully
            class MockPutSuccessContextManager:
                async def __aenter__(self):
                    return MockResponse()
                async def __aexit__(self, exc_type, exc, tb):
                    pass

            def mock_put_success(*args, **kwargs):
                return MockPutSuccessContextManager()
                
            with patch("aiohttp.ClientSession.put", new=mock_put_success), patch("app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt):
                await process_oc_sync()

            # 10. Assert final state
            async with GetDB() as db7:
                mapping_final = (await db7.execute(select(OCUserMapping).where(OCUserMapping.id == mapping.id))).scalar_one()
                assert mapping_final.last_synced_configs == ["config-a", "config-b", "config-c"]
                
                job_final = (await db7.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalar_one()
                assert job_final.status == "completed"
                
            print("   [x] Latest state executed successfully!")

        finally:
            # Cleanup
            async with GetDB() as db:
                for entity in [integration, panel, user, group, inbound1, inbound2, inbound3, config1, config2, config3]:
                    try:
                        await db.delete(entity)
                    except Exception:
                        pass
                await db.commit()
                await db.execute(OCSyncState.__table__.delete())
                await db.commit()

if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
