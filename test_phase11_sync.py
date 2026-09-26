import asyncio
import uuid
import sys
import datetime
if not hasattr(datetime, 'UTC'):
    datetime.UTC = datetime.timezone.utc
from datetime import UTC, datetime as dt
from sqlalchemy import select, delete

from app.db import GetDB
from app.db.models import User, Group, ProxyInbound, Admin, ProxyHost
from app.db.models_oc import OCUserMapping, OCSyncState, OCPanel, OCPanelConfig, OCPanelGroup, OCIntegration
from app.node.oc_sync import sync_panel_from_outbound_center, enqueue_oc_user_sync
from app.utils.crypto import encrypt_secret

class MockResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status
    
    async def json(self):
        return self.data
    
    async def text(self):
        return str(self.data)
        
    def raise_for_status(self):
        if self.status >= 400:
            raise Exception("HTTP Error")

mock_oc_state = {
    "panel": {"id": 1, "panel_type": "xui", "name": "Test Panel", "status": "active"},
    "groups": {"groups": [
        {"id": "g1", "name": "Group 1"},
        {"id": "g2", "name": "Group 2"}
    ]},
    "configs": {"configs": [
        {"id": "c1", "name": "Config 1", "group_mapping": {"supported": True, "groups": ["g1"]}},
        {"id": "c2", "name": "Config 2", "group_mapping": {"supported": True, "groups": ["g2"]}}
    ]}
}

fail_on_configs = False

async def mock_call_oc_api(integration, method, path, json=None):
    global fail_on_configs
    if "/configs" in path:
        if fail_on_configs:
            raise RuntimeError("Simulated Outbound Center API Failure on /configs")
        return mock_oc_state["configs"]
    elif "/groups" in path:
        return mock_oc_state["groups"]
    elif "/panels/" in path:
        return mock_oc_state["panel"]
    return None

import app.routers.integration
app.routers.integration.call_oc_api = mock_call_oc_api

async def run_tests():
    global fail_on_configs
    print("Running Phase 11 Tests...")
    
    async with GetDB() as db:
        test_admin = (await db.execute(select(Admin).limit(1))).scalar_one_or_none()
        if not test_admin:
            test_admin = Admin(username="test_admin", hashed_password="pw")
            db.add(test_admin)
            await db.commit()
            
    entities_to_delete = []
    
    try:
        async with GetDB() as db:
            # Setup
            token = await encrypt_secret("test")
            integration = OCIntegration(base_url="http://test", api_token_encrypted=token, token_preview="test")
            db.add(integration)
            await db.flush()
            
            panel = OCPanel(
                integration_id=integration.id,
                source_panel_id="1",
                purchaser_identity="test",
                name="Test Panel",
                multiplier=2.5
            )
            db.add(panel)
            await db.flush()
            entities_to_delete.extend([integration, panel])

            # --- TEST L: Phase 10 User Mapping Preservation ---
            test_user = User(username=f"test_u_{uuid.uuid4().hex[:6]}", status="active")
            db.add(test_user)
            await db.flush()
            entities_to_delete.append(test_user)

            user_mapping = OCUserMapping(
                user_id=test_user.id,
                panel_id=panel.id,
                external_user_id=f"{test_user.id}_p{panel.id}",
                status="active",
                last_synced_configs=["c1"]
            )
            db.add(user_mapping)
            await db.commit()
            mapping_id = user_mapping.id
            
            # --- TEST A: Initial Sync & B: New Group & C: New Config ---
            await sync_panel_from_outbound_center(db, panel.id)
            
            # Verify panel sync
            await db.refresh(panel, ["groups", "configs"])
            assert len(panel.groups) == 2
            assert len(panel.configs) == 2
            assert panel.multiplier == 2.5 # Test M: Multiplier unchanged
            
            g1 = next(g for g in panel.groups if g.source_group_id == "g1")
            g2 = next(g for g in panel.groups if g.source_group_id == "g2")
            assert g1.source_name == "Group 1"
            
            c1 = next(c for c in panel.configs if c.source_config_id == "c1")
            assert c1.source_name == "Config 1"
            assert c1.panel_group_id == g1.id
            
            # Check host/inbound
            host1 = (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == c1.virtual_inbound_tag))).scalar_one()
            assert host1.remark == "Config 1"

            # Assert TEST L: Phase 10 User Mapping is completely preserved
            refreshed_mapping = (await db.execute(
                select(OCUserMapping).where(OCUserMapping.id == mapping_id)
            )).scalar_one_or_none()
            assert refreshed_mapping is not None, "Mapping must still exist"
            assert refreshed_mapping.id == mapping_id, "Mapping primary identity must be unchanged"
            assert refreshed_mapping.user_id == test_user.id, "user_id must be unchanged"
            assert refreshed_mapping.panel_id == panel.id, "panel_id must be unchanged"
            assert refreshed_mapping.external_user_id == f"{test_user.id}_p{panel.id}", "external_user_id must be unchanged"
            assert refreshed_mapping.status == "active", "status must be unchanged"
            assert refreshed_mapping.last_synced_configs == ["c1"], "last_synced_configs must be unchanged"

            all_mappings = (await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == test_user.id, OCUserMapping.panel_id == panel.id)
            )).scalars().all()
            assert len(all_mappings) == 1, "No duplicate mapping row should be created"
            
            print("   [x] Initial Sync & Phase 10 Mapping Preservation OK")
            
            # --- TEST A, K: Repeated Synchronization creates no duplicates ---
            await sync_panel_from_outbound_center(db, panel.id)
            await db.refresh(panel, ["groups", "configs"])
            assert len(panel.groups) == 2
            assert len(panel.configs) == 2
            
            print("   [x] Idempotency OK")
            
            # --- TEST E, F, N: Config Rename preserves identity, tag, and host remark if changed ---
            mock_oc_state["configs"]["configs"][0]["name"] = "Config 1 Renamed"
            # Simulate user changed remark
            host1.remark = "User Custom Remark"
            await db.commit()
            
            await sync_panel_from_outbound_center(db, panel.id)
            await db.refresh(panel, ["configs"])
            
            c1 = next(c for c in panel.configs if c.source_config_id == "c1")
            assert c1.source_name == "Config 1 Renamed"
            assert c1.virtual_inbound_tag == host1.inbound_tag # Tag preserved
            
            host1_after = (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == c1.virtual_inbound_tag))).scalar_one()
            assert host1_after.remark == "User Custom Remark", "Phase 7 host remark must remain intact"
            
            print("   [x] Config Rename & Host Remark Preservation OK")
            
            # --- TEST G: Config moves between groups ---
            mock_oc_state["configs"]["configs"][0]["group_mapping"]["groups"] = ["g2"]
            await sync_panel_from_outbound_center(db, panel.id)
            await db.refresh(panel, ["configs"])
            
            c1 = next(c for c in panel.configs if c.source_config_id == "c1")
            assert c1.panel_group_id == g2.id
            assert len(panel.configs) == 2
            
            print("   [x] Group Reassignment OK")
            
            # --- TEST H, I: Removed Config / Group ---
            # Remove c2 and g2 from OC
            mock_oc_state["configs"]["configs"] = [mock_oc_state["configs"]["configs"][0]]
            mock_oc_state["groups"]["groups"] = [{"id": "g1", "name": "Group 1"}]
            
            await sync_panel_from_outbound_center(db, panel.id)
            await db.refresh(panel, ["groups", "configs"])
            
            assert len(panel.groups) == 2 # Group should not be hard-deleted
            assert len(panel.configs) == 2 # Config should not be hard-deleted
            
            c2 = next(c for c in panel.configs if c.source_config_id == "c2")
            assert c2.source_missing == True
            
            print("   [x] Safe Lifecycle Behavior OK")

            # --- TEST O: Concurrent Synchronization ---
            # Add a new group and a new config to simulate real race condition where both syncs discover new items
            mock_oc_state["groups"]["groups"].append({"id": "g_conc", "name": "Group Concurrent"})
            mock_oc_state["configs"]["configs"].append({
                "id": "c_conc",
                "name": "Config Concurrent",
                "group_mapping": {"supported": True, "groups": ["g_conc"]}
            })

            async def do_concurrent_sync():
                async with GetDB() as sess:
                    await sync_panel_from_outbound_center(sess, panel.id)

            # Execute simultaneously in two separate database sessions
            await asyncio.gather(
                do_concurrent_sync(),
                do_concurrent_sync()
            )

            await db.refresh(panel, ["groups", "configs"])
            # Assert exactly one group per (panel_id, source_group_id)
            conc_groups = [g for g in panel.groups if g.source_group_id == "g_conc"]
            assert len(conc_groups) == 1, f"Expected exactly 1 concurrent group, got {len(conc_groups)}"

            # Assert exactly one config per (panel_id, source_config_id)
            conc_configs = [c for c in panel.configs if c.source_config_id == "c_conc"]
            assert len(conc_configs) == 1, f"Expected exactly 1 concurrent config, got {len(conc_configs)}"

            # Assert exactly one virtual inbound and host
            inbounds = (await db.execute(
                select(ProxyInbound).where(ProxyInbound.tag == f"oc_{panel.id}_c_conc")
            )).scalars().all()
            assert len(inbounds) == 1, f"Expected exactly 1 inbound, got {len(inbounds)}"

            hosts = (await db.execute(
                select(ProxyHost).where(ProxyHost.inbound_tag == f"oc_{panel.id}_c_conc")
            )).scalars().all()
            assert len(hosts) == 1, f"Expected exactly 1 host, got {len(hosts)}"

            print("   [x] Concurrent Synchronization OK")

            # --- TEST P: Transaction Rollback on Failure ---
            # Stage a new group in Outbound Center, but force failure on configs fetch
            mock_oc_state["groups"]["groups"].append({"id": "g_fail", "name": "Group To Rollback"})
            fail_on_configs = True

            sync_failed = False
            try:
                async with GetDB() as fail_db:
                    await sync_panel_from_outbound_center(fail_db, panel.id)
            except RuntimeError as e:
                sync_failed = True
                assert "Simulated Outbound Center API Failure" in str(e)
            finally:
                fail_on_configs = False

            assert sync_failed, "Synchronization must raise on external API failure"

            # Open a completely fresh session to verify rollback
            async with GetDB() as fresh_db:
                staged_group = (await fresh_db.execute(
                    select(OCPanelGroup).where(
                        OCPanelGroup.panel_id == panel.id,
                        OCPanelGroup.source_group_id == "g_fail"
                    )
                )).scalar_one_or_none()
                assert staged_group is None, "Partially flushed group must NOT be committed to the database"

                # Also verify no stray configs or inbounds were committed
                stray_inbound = (await fresh_db.execute(
                    select(ProxyInbound).where(ProxyInbound.tag == f"oc_{panel.id}_c_fail")
                )).scalar_one_or_none()
                assert stray_inbound is None, "No stray inbound should be committed"

            print("   [x] Transaction Rollback OK")
            
            print("\nALL PHASE 11 TESTS PASSED SUCCESSFULLY!")
            
    finally:
        async with GetDB() as db:
            if 'panel' in locals() and panel:
                panel_tags = (await db.execute(
                    select(ProxyInbound.tag).where(ProxyInbound.tag.like(f"oc_{panel.id}_%"))
                )).scalars().all()
                if panel_tags:
                    await db.execute(delete(ProxyHost).where(ProxyHost.inbound_tag.in_(panel_tags)))
                    await db.execute(delete(ProxyInbound).where(ProxyInbound.tag.in_(panel_tags)))

            for entity in reversed(entities_to_delete):
                try:
                    await db.delete(entity)
                except Exception:
                    pass
            await db.commit()

if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
