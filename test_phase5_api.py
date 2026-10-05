import asyncio
from datetime import timezone, datetime
from fastapi import HTTPException
from sqlalchemy import select, delete

from app.db import GetDB
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCPanelGroup
from app.db.models import ProxyInbound, ProxyHost
from app.routers.panel import get_current_user_context
from app.utils.jwt import create_admin_token, create_customer_token
from app.utils.crypto import encrypt_secret
from app.routers.integration import select_panel, create_test_user, get_groups, sync_configs_and_hosts, SelectPanelRequest, SyncRequest

class MockRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}

# To mock call_oc_api, we need to monkeypatch it.
import app.routers.integration as integration_router

async def mock_call_oc_api(integration, method, path, json=None, **kwargs):
    if "test-user" in path:
        return {"external_user_id": "pasarguard_discovery_mock"}
    elif method == "PUT" and "/users/" in path:
        return {"subscription_url": "https://oc.example/sub/mock"}
    elif "groups" in path:
        return {"groups": [{"id": "g1", "name": "Group A"}, {"id": "g2", "name": "Group B"}]}
    elif "configs" in path:
        return {"configs": [
            {"id": "cfg1", "name": "Config 1", "group_mapping": {"supported": True, "groups": ["g1"]}},
            {"id": "cfg2", "name": "Config 2", "group_mapping": {"supported": True, "groups": ["g1"]}},
            {"id": "cfg3", "name": "Config 3", "group_mapping": {"supported": True, "groups": ["g2"]}},
        ]}
    elif "panels" in path or "accounts" in path:
        return {
            "items": [
                {
                    "subscription_id": 40,
                    "panel_type": "marzban",
                    "name": "admin - Sub 40",
                    "status": "active",
                }
            ]
        }
    return {}

import app.services.oc_connection_credentials as oc_creds


def _install_oc_api_mocks() -> None:
    integration_router.call_oc_api = mock_call_oc_api
    oc_creds.call_oc_api = mock_call_oc_api


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
    await db.execute(delete(OCPanel).where(OCPanel.source_panel_id == "40"))
    await db.commit()
    return integration.id

async def run_tests():
    _install_oc_api_mocks()
    print("Running Phase 5 Tests...")
    async with GetDB() as db:
        await setup_test_data(db)
        owner_token = await create_admin_token(1, "admin")
        req = MockRequest()
        owner_ctx = await get_current_user_context(req, db, token=owner_token)
        
        print("1. Purchased Panel Selection...")
        select_req = SelectPanelRequest(source_panel_id="40", name="admin - Sub 40")
        resp = await select_panel(select_req, db, owner_ctx)
        panel_id = resp.panel_id
        assert resp.source_panel_id == "40"
        print("   [x] Panel selected and created.")

        print("2. Duplicate Panel Selection rejected...")
        from fastapi import HTTPException
        try:
            await select_panel(select_req, db, owner_ctx)
            assert False, "expected 409 for already-imported panel"
        except HTTPException as exc:
            assert exc.status_code == 409
        print("   [x] Duplicate panel selection rejected.")
        
        print("3. Create Test User...")
        t_resp = await create_test_user(panel_id, db, owner_ctx)
        assert t_resp.test_user_id == "pasarguard_discovery_mock"
        print("   [x] Test user created.")
        
        print("4. Idempotent Test User...")
        t_resp2 = await create_test_user(panel_id, db, owner_ctx)
        assert t_resp2.test_user_id == "pasarguard_discovery_mock"
        print("   [x] Duplicate test user creation prevented.")
        
        print("5. Fetch Groups...")
        g_resp = await get_groups(panel_id, db, owner_ctx)
        assert len(g_resp.groups) == 2
        print("   [x] Groups fetched.")
        
        print("6. Sync Configs and Create Hosts...")
        sync_req = SyncRequest(selected_group_ids=["g1"], group_names={"g1": "Group A"})
        from unittest.mock import AsyncMock, patch

        sub_body = "\n".join(
            [
                "vless://u1@1.1.1.1:443?encryption=none#Config%201",
                "vless://u2@2.2.2.2:443?encryption=none#Config%202",
            ]
        )
        async def mock_call_oc_panel_api(db, panel, method, path, json=None):
            return await mock_call_oc_api(panel.integration, method, path, json)

        with (
            patch(
                "app.services.oc_panel_host_subscription.call_oc_panel_api",
                new_callable=AsyncMock,
                side_effect=mock_call_oc_panel_api,
            ),
            patch(
                "app.services.oc_panel_host_subscription.fetch_upstream_subscription_body",
                new_callable=AsyncMock,
                return_value=sub_body,
            ),
        ):
            sync_resp = await sync_configs_and_hosts(panel_id, sync_req, db, owner_ctx)
        assert sync_resp.configs_created == 2
        assert sync_resp.hosts_created == 2
        print("   [x] Configs and destination hosts created for selected groups.")
        
        print("7. Duplicate Config/Host prevention...")
        sync_resp2 = await sync_configs_and_hosts(panel_id, sync_req, db, owner_ctx)
        assert sync_resp2.configs_created == 0
        assert sync_resp2.hosts_created == 0
        print("   [x] Duplicates prevented.")
        
        print("8. Check Inbound Tag isolation...")
        configs = (await db.execute(select(OCPanelConfig).where(OCPanelConfig.panel_id == panel_id))).scalars().all()
        assert len(configs) == 2
        assert configs[0].source_config_id != configs[1].source_config_id
        assert all(c.locally_hidden for c in configs)
        print("   [x] Catalog metadata rows isolated per config.")

        print("ALL TESTS PASSED.")

if __name__ == "__main__":
    asyncio.run(run_tests())
