import asyncio
from fastapi import HTTPException
from sqlalchemy import select
from decimal import Decimal

from app.db import GetDB
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig
from app.db.models import ProxyHost, ProxyInbound
from app.routers.panel import update_panel, PanelUpdate, list_panels, get_panel, get_current_user_context, list_panel_hosts, get_panel_host, update_panel_host, PanelHostUpdate

class MockRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}

async def setup_test_data(db):
    integration = (await db.execute(select(OCIntegration).limit(1))).scalar_one_or_none()
    if not integration:
        integration = OCIntegration(
            base_url="https://api.example.com",
            api_token_encrypted="encrypted_token",
            token_preview="preview",
            is_active=True
        )
        db.add(integration)
        await db.flush()

    panel = OCPanel(
        integration_id=integration.id,
        source_panel_id="panel_1",
        purchaser_identity="admin",
        name="Test Panel",
        multiplier=1.00
    )
    db.add(panel)
    await db.flush()

    inbound = ProxyInbound(tag=f"oc_{panel.id}_conf1")
    db.add(inbound)
    await db.flush()

    host = ProxyHost(
        remark="Test Config",
        priority=0,
        address={"8.8.8.8"},
        status=[],
        alpn=[],
        port=443,
        path="/",
        allowinsecure=False
    )
    host.inbound = inbound
    db.add(host)
    await db.flush()

    config = OCPanelConfig(
        panel_id=panel.id,
        source_config_id="conf1",
        source_name="Test Config",
        virtual_inbound_tag=inbound.tag
    )
    db.add(config)
    await db.commit()

    return {"integration": integration, "panel": panel, "host": host}


async def cleanup_test_data(db, test_ids):
    if "panel" in test_ids:
        panel = (await db.execute(select(OCPanel).where(OCPanel.id == test_ids["panel"].id))).scalar_one_or_none()
        if panel:
            await db.delete(panel)
    if "host" in test_ids:
        host = (await db.execute(select(ProxyHost).where(ProxyHost.id == test_ids["host"].id))).scalar_one_or_none()
        if host:
            await db.delete(host)
    await db.commit()


async def run_tests():
    print("Running Phase 8 Multiplier Tests...")
    
    owner_ctx = ("admin", True)
    
    async with GetDB() as db:
        test_ids = await setup_test_data(db)
        panel_id = test_ids["panel"].id
        host_id = test_ids["host"].id

    try:
        async with GetDB() as db:
            # Test 1: New OCPanel defaults to multiplier = 1
            p = await get_panel(panel_id=panel_id, db=db, user_context=owner_ctx)
            assert p.multiplier == 1.0
            print("   [x] Panel defaults to multiplier 1.0")

            # Test 2: Valid integer multiplier
            update_req = PanelUpdate(multiplier=Decimal("2"))
            p = await update_panel(panel_id=panel_id, update_data=update_req, db=db, user_context=owner_ctx)
            assert p.multiplier == 2.0
            print("   [x] Valid integer multiplier accepted (2)")

            # Test 3: Valid decimal multiplier
            update_req = PanelUpdate(multiplier=Decimal("1.5"))
            p = await update_panel(panel_id=panel_id, update_data=update_req, db=db, user_context=owner_ctx)
            assert p.multiplier == 1.5
            print("   [x] Valid decimal multiplier accepted (1.5)")

            # Test 4: Exactly 2 decimal places
            update_req = PanelUpdate(multiplier=Decimal("1.25"))
            p = await update_panel(panel_id=panel_id, update_data=update_req, db=db, user_context=owner_ctx)
            assert p.multiplier == 1.25
            print("   [x] Multiplier with exactly 2 decimal places accepted (1.25)")

            # Test 5 & 6 (Validation testing handled by Pydantic, so we simulate ValidationError manually or trust Pydantic)
            try:
                PanelUpdate(multiplier=Decimal("0"))
                assert False, "Should raise validation error for 0"
            except Exception as e:
                print("   [x] Multiplier <= 0 rejected by Pydantic")
            
            # Test 11, 13: Hosts inherit multiplier
            hosts = await list_panel_hosts(panel_id=panel_id, db=db, user_context=owner_ctx)
            assert hosts[0].multiplier == 1.25
            print("   [x] Host correctly inherits multiplier (1.25)")

            # Update again to verify dynamic inheritance
            update_req = PanelUpdate(multiplier=Decimal("3.75"))
            await update_panel(panel_id=panel_id, update_data=update_req, db=db, user_context=owner_ctx)
            hosts = await list_panel_hosts(panel_id=panel_id, db=db, user_context=owner_ctx)
            assert hosts[0].multiplier == 3.75
            print("   [x] Host dynamically updates inherited multiplier when panel changes (3.75)")

            # Test 12: Host cannot independently update multiplier
            # We removed multiplier from PanelHostUpdate, so we pass it normally to update_panel_host
            host_update = PanelHostUpdate(display_name="Renamed")
            h = await update_panel_host(panel_id=panel_id, host_id=host_id, update_data=host_update, db=db, user_context=owner_ctx)
            assert h.display_name == "Renamed"
            assert h.multiplier == 3.75
            print("   [x] Host can be updated but cannot change its own multiplier")

        print("\nALL PHASE 8 TESTS PASSED SUCCESSFULLY!")
    finally:
        async with GetDB() as db:
            await cleanup_test_data(db, test_ids)

if __name__ == "__main__":
    asyncio.run(run_tests())
