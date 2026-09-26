import asyncio
import uuid
import sys
import datetime
if not hasattr(datetime, 'UTC'):
    datetime.UTC = datetime.timezone.utc
from datetime import UTC, datetime as dt
from sqlalchemy import select

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

async def mock_call_oc_api(integration, method, path, json=None):
    if "/configs" in path:
        return mock_oc_state["configs"]
    elif "/groups" in path:
        return mock_oc_state["groups"]
    elif "/panels/" in path:
        return mock_oc_state["panel"]
    return None

import app.routers.integration
app.routers.integration.call_oc_api = mock_call_oc_api

async def run_tests():
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
            
            # --- TEST A: Initial Sync & B: New Group & C: New Config ---
            await sync_panel_from_outbound_center(db, panel.id)
            
            # Verify
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
            
            print("   [x] Initial Sync OK")
            
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
            
            print("\nALL PHASE 11 TESTS PASSED SUCCESSFULLY!")
            
    finally:
        async with GetDB() as db:
            for entity in reversed(entities_to_delete):
                try:
                    await db.delete(entity)
                except Exception:
                    pass
            await db.commit()

if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
