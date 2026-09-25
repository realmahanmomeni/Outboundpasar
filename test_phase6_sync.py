import asyncio
from datetime import timezone, datetime
from fastapi import HTTPException
from sqlalchemy import select, delete

from app.db import GetDB
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCPanelGroup
from app.db.models import ProxyInbound, ProxyHost
from app.routers.panel import get_current_user_context
from app.utils.jwt import create_admin_token
from app.utils.crypto import encrypt_secret
from app.routers.integration import select_panel, sync_configs_and_hosts, SelectPanelRequest, SyncRequest

class MockRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}

import app.routers.integration as integration_router

# State for the mock OC API
OC_API_STATE = {
    "groups": [{"id": "g1", "name": "Group A"}, {"id": "g2", "name": "Group B"}],
    "configs": [
        {"id": "cfg1", "name": "Config 1", "group_mapping": {"supported": True, "groups": ["g1"]}},
        {"id": "cfg2", "name": "Config 2", "group_mapping": {"supported": True, "groups": ["g1"]}},
        {"id": "cfg3", "name": "Config 3", "group_mapping": {"supported": True, "groups": ["g2"]}},
    ],
    "panels": [{"id": 40, "panel_type": "marzban", "name": "admin - Sub 40", "status": "active"}]
}

async def mock_call_oc_api(integration, method, path, json=None):
    if "test-user" in path:
        return {"external_user_id": "pasarguard_discovery_mock"}
    elif "groups" in path:
        return {"groups": OC_API_STATE["groups"]}
    elif "configs" in path:
        return {"configs": OC_API_STATE["configs"]}
    elif "panels" in path:
        return {"items": OC_API_STATE["panels"]}
    return {}

integration_router.call_oc_api = mock_call_oc_api

async def setup_test_data(db):
    integration = (await db.execute(select(OCIntegration).limit(1))).scalar_one_or_none()
    if not integration:
        enc_token = await encrypt_secret("test-token-123")
        integration = OCIntegration(
            base_url="https://oc.example.com",
            api_token_encrypted=enc_token,
            token_preview="test...123",
            is_active=True,
        )
        db.add(integration)
        await db.commit()
        await db.refresh(integration)
    
    await db.execute(delete(OCPanelConfig).where(OCPanelConfig.source_name.like("Config %")))
    await db.execute(delete(OCPanelGroup))
    await db.execute(delete(OCPanel).where(OCPanel.source_panel_id.in_(["40", "41"])))
    await db.commit()
    return integration.id

async def run_tests():
    print("Running Phase 6 Tests...")
    async with GetDB() as db:
        await setup_test_data(db)
        owner_token = await create_admin_token(1, "admin")
        owner_ctx = await get_current_user_context(MockRequest(), db, token=owner_token)
        
        # 1. Panel Isolation Setup
        print("Setup: Create Panel 40 and Panel 41...")
        p40_req = SelectPanelRequest(source_panel_id="40", name="admin - Sub 40")
        p41_req = SelectPanelRequest(source_panel_id="41", name="admin - Sub 41")
        p40_resp = await select_panel(p40_req, db, owner_ctx)
        p41_resp = await select_panel(p41_req, db, owner_ctx)
        p40_id = p40_resp.panel_id
        p41_id = p41_resp.panel_id
        
        # 2. First Sync (Panel 40)
        print("Test: First Sync (Panel 40)...")
        sync_req = SyncRequest(
            selected_group_ids=["g1", "g2"],
            group_names={"g1": "Group A", "g2": "Group B"}
        )
        resp1 = await sync_configs_and_hosts(p40_id, sync_req, db, owner_ctx)
        assert resp1.configs_created == 3
        assert resp1.hosts_created == 3
        
        # Check groups
        groups = (await db.execute(select(OCPanelGroup).where(OCPanelGroup.panel_id == p40_id))).scalars().all()
        assert len(groups) == 2
        g1_db = next(g for g in groups if g.source_group_id == "g1")
        g2_db = next(g for g in groups if g.source_group_id == "g2")
        
        # Check configs group assignment
        configs = (await db.execute(select(OCPanelConfig).where(OCPanelConfig.panel_id == p40_id))).scalars().all()
        cfg1 = next(c for c in configs if c.source_config_id == "cfg1")
        cfg3 = next(c for c in configs if c.source_config_id == "cfg3")
        assert cfg1.panel_group_id == g1_db.id
        assert cfg3.panel_group_id == g2_db.id
        
        # 3. Repeated Sync (Idempotency)
        print("Test: Repeated Sync (Idempotency)...")
        resp2 = await sync_configs_and_hosts(p40_id, sync_req, db, owner_ctx)
        assert resp2.configs_created == 0
        assert resp2.hosts_created == 0
        
        # 4. Config moving between Groups & Name Update
        print("Test: Config moving between Groups & Config Name Update...")
        OC_API_STATE["configs"][0]["group_mapping"]["groups"] = ["g2"] # cfg1 moves to g2
        OC_API_STATE["configs"][0]["name"] = "Config 1 - Renamed"
        
        resp3 = await sync_configs_and_hosts(p40_id, sync_req, db, owner_ctx)
        assert resp3.configs_created == 0
        assert resp3.hosts_created == 0
        
        await db.refresh(cfg1)
        assert cfg1.source_name == "Config 1 - Renamed"
        assert cfg1.panel_group_id == g2_db.id
        
        # Check Host remark update
        host1 = (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == f"oc_{p40_id}_cfg1"))).scalar_one()
        assert host1.remark == "Config 1 - Renamed"
        
        # 5. New Config Added
        print("Test: New Config Added...")
        OC_API_STATE["configs"].append({"id": "cfg4", "name": "Config 4", "group_mapping": {"supported": True, "groups": ["g1"]}})
        resp4 = await sync_configs_and_hosts(p40_id, sync_req, db, owner_ctx)
        assert resp4.configs_created == 1
        assert resp4.hosts_created == 1
        
        # 6. Group Name Update
        print("Test: Group Name Update...")
        sync_req.group_names["g1"] = "Group A - Renamed"
        resp5 = await sync_configs_and_hosts(p40_id, sync_req, db, owner_ctx)
        await db.refresh(g1_db)
        assert g1_db.source_name == "Group A - Renamed"
        
        # 7. Panel Isolation
        print("Test: Panel Isolation (Panel 41)...")
        # Syncing Panel 41 should create its own groups and configs independently
        resp6 = await sync_configs_and_hosts(p41_id, sync_req, db, owner_ctx)
        assert resp6.configs_created == 4
        assert resp6.hosts_created == 4
        
        p41_groups = (await db.execute(select(OCPanelGroup).where(OCPanelGroup.panel_id == p41_id))).scalars().all()
        assert len(p41_groups) == 2
        p41_g1 = next(g for g in p41_groups if g.source_group_id == "g1")
        assert p41_g1.id != g1_db.id # Separate database row!
        
        p41_configs = (await db.execute(select(OCPanelConfig).where(OCPanelConfig.panel_id == p41_id))).scalars().all()
        assert len(p41_configs) == 4
        p41_cfg1 = next(c for c in p41_configs if c.source_config_id == "cfg1")
        assert p41_cfg1.id != cfg1.id # Separate database row!
        assert p41_cfg1.virtual_inbound_tag == f"oc_{p41_id}_cfg1"
        assert cfg1.virtual_inbound_tag == f"oc_{p40_id}_cfg1"
        
        print("ALL TESTS PASSED.")

if __name__ == "__main__":
    asyncio.run(run_tests())
