import asyncio
from datetime import UTC, datetime
from fastapi import HTTPException
from sqlalchemy import select, delete

from app.db import GetDB
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig
from app.routers.panel import list_panels, get_panel, get_current_user_context
from app.utils.jwt import create_admin_token, create_customer_token
from app.utils.crypto import encrypt_secret


class MockRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


async def setup_test_data(db):
    # Ensure test integration
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

    # Clean existing test panels
    await db.execute(delete(OCPanelConfig).where(OCPanelConfig.source_name.like("test-%")))
    await db.execute(delete(OCPanel).where(OCPanel.name.like("test-%")))
    await db.commit()

    # Panel 1: Purchased by Mahan (Panel 40, instance 1)
    panel_mahan_1 = OCPanel(
        integration_id=integration.id,
        source_panel_id="40",
        purchaser_identity="Mahan",
        name="test-Panel 40 - Mahan #1",
        multiplier=1.5,
        sync_status="connected",
    )
    db.add(panel_mahan_1)
    await db.commit()
    await db.refresh(panel_mahan_1)

    # Panel 2: Purchased by Mahan (Panel 40, instance 2 - second purchase)
    panel_mahan_2 = OCPanel(
        integration_id=integration.id,
        source_panel_id="40",
        purchaser_identity="Mahan",
        name="test-Panel 40 - Mahan #2",
        multiplier=2.0,
        sync_status="pending",
    )
    db.add(panel_mahan_2)

    # Panel 3: Purchased by Ali (Panel 40, instance 3 - Ali's purchase)
    panel_ali = OCPanel(
        integration_id=integration.id,
        source_panel_id="40",
        purchaser_identity="Ali",
        name="test-Panel 40 - Ali",
        multiplier=1.0,
        sync_status=None,  # Not connected
    )
    db.add(panel_ali)
    await db.commit()
    await db.refresh(panel_mahan_2)
    await db.refresh(panel_ali)

    # Add configs to panel_mahan_1 (count = 2)
    cfg1 = OCPanelConfig(
        panel_id=panel_mahan_1.id,
        source_config_id="cfg-1",
        source_name="test-config-1",
    )
    cfg2 = OCPanelConfig(
        panel_id=panel_mahan_1.id,
        source_config_id="cfg-2",
        source_name="test-config-2",
    )
    db.add(cfg1)
    db.add(cfg2)
    await db.commit()

    return {
        "integration_id": integration.id,
        "panel_mahan_1_id": panel_mahan_1.id,
        "panel_mahan_2_id": panel_mahan_2.id,
        "panel_ali_id": panel_ali.id,
    }


async def cleanup_test_data(db):
    await db.execute(delete(OCPanelConfig).where(OCPanelConfig.source_name.like("test-%")))
    await db.execute(delete(OCPanel).where(OCPanel.name.like("test-%")))
    await db.commit()


async def run_tests():
    async with GetDB() as db:
        test_ids = await setup_test_data(db)

    try:
        req = MockRequest()

        print("1. Testing unauthenticated request...")
        try:
            async with GetDB() as db:
                await get_current_user_context(req, db, token=None)
            assert False, "Should have raised 401"
        except HTTPException as e:
            assert e.status_code == 401
            print("   [x] Unauthenticated request correctly rejected with 401.")

        print("\n2. Testing authentication & user context resolution...")
        owner_token = await create_admin_token(1, "admin")
        async with GetDB() as db:
            owner_ctx = await get_current_user_context(req, db, token=owner_token)
            assert owner_ctx[1] is True, "Admin should be detected as owner"
            print("   [x] Owner admin token resolved as owner=True.")

        token_mahan = await create_customer_token("Mahan")
        async with GetDB() as db:
            mahan_ctx = await get_current_user_context(req, db, token=token_mahan)
            assert mahan_ctx == ("Mahan", False)
            print("   [x] Customer token resolved with identity='Mahan', owner=False.")

        token_ali = await create_customer_token("Ali")
        async with GetDB() as db:
            ali_ctx = await get_current_user_context(req, db, token=token_ali)
            assert ali_ctx == ("Ali", False)
            print("   [x] Customer token resolved with identity='Ali', owner=False.")

        print("\n3. Testing Owner listing all panels...")
        async with GetDB() as db:
            all_panels = await list_panels(db=db, user_context=owner_ctx)
            test_panels = [p for p in all_panels if p.name.startswith("test-")]
            assert len(test_panels) == 3, f"Owner should see all 3 test panels, got {len(test_panels)}"
            print("   [x] Owner saw all panel instances.")

            p_m1 = next(p for p in test_panels if p.id == test_ids["panel_mahan_1_id"])
            p_m2 = next(p for p in test_panels if p.id == test_ids["panel_mahan_2_id"])
            p_ali = next(p for p in test_panels if p.id == test_ids["panel_ali_id"])

            # Identity
            assert p_m1.source_panel_id == "40"
            assert p_m2.source_panel_id == "40"
            assert p_ali.source_panel_id == "40"
            assert p_m1.id != p_m2.id, "Different instances of Panel 40 must have unique IDs"
            assert p_m1.purchaser_identity == "Mahan"
            assert p_m2.purchaser_identity == "Mahan"
            assert p_ali.purchaser_identity == "Ali"
            print("   [x] Multiple purchases of same source panel are preserved as distinct instances.")

            # Config Count
            assert p_m1.configs_count == 2, f"Expected 2 configs, got {p_m1.configs_count}"
            assert p_m2.configs_count == 0, f"Expected 0 configs, got {p_m2.configs_count}"
            print("   [x] Config count accurately reflects imported configs.")

            # Multiplier
            assert p_m1.multiplier == 1.5, f"Expected 1.5, got {p_m1.multiplier}"
            assert p_m2.multiplier == 2.0, f"Expected 2.0, got {p_m2.multiplier}"
            print("   [x] Multiplier accurately displayed.")

            # Honest Status
            assert p_m1.sync_status == "connected", f"Expected connected, got {p_m1.sync_status}"
            assert p_m2.sync_status == "pending", f"Expected pending, got {p_m2.sync_status}"
            assert p_ali.sync_status is None, f"Expected None, got {p_ali.sync_status}"
            print("   [x] Status is honest and not fabricated.")

        print("\n4. Testing Account Isolation for Mahan...")
        async with GetDB() as db:
            mahan_panels = await list_panels(db=db, user_context=mahan_ctx)
            mahan_test = [p for p in mahan_panels if p.name.startswith("test-")]
            assert len(mahan_test) == 2, f"Mahan should only see 2 panels, got {len(mahan_test)}"
            assert all(p.purchaser_identity == "Mahan" for p in mahan_test)
            print("   [x] Mahan sees only Mahan's panels.")

        print("\n5. Testing Account Isolation for Ali...")
        async with GetDB() as db:
            ali_panels = await list_panels(db=db, user_context=ali_ctx)
            ali_test = [p for p in ali_panels if p.name.startswith("test-")]
            assert len(ali_test) == 1, f"Ali should only see 1 panel, got {len(ali_test)}"
            assert ali_test[0].purchaser_identity == "Ali"
            print("   [x] Ali sees only Ali's panel.")

        print("\n6. Testing Cross-Account Detail Access...")
        async with GetDB() as db:
            # Ali tries to access Mahan's panel
            try:
                await get_panel(panel_id=test_ids["panel_mahan_1_id"], db=db, user_context=ali_ctx)
                assert False, "Ali should be forbidden from accessing Mahan's panel"
            except HTTPException as e:
                assert e.status_code == 403
                print("   [x] Ali blocked from Mahan's panel (403 Forbidden).")

            # Mahan accesses own panel
            p = await get_panel(panel_id=test_ids["panel_mahan_1_id"], db=db, user_context=mahan_ctx)
            assert p.id == test_ids["panel_mahan_1_id"]
            print("   [x] Mahan successfully accessed own panel.")

            # Owner accesses Mahan's panel
            p_owner = await get_panel(panel_id=test_ids["panel_mahan_1_id"], db=db, user_context=owner_ctx)
            assert p_owner.id == test_ids["panel_mahan_1_id"]
            print("   [x] Owner successfully accessed panel.")

        print("\nALL PHASE 4 BACKEND API TESTS PASSED SUCCESSFULLY!")
    finally:
        async with GetDB() as db:
            await cleanup_test_data(db)


if __name__ == "__main__":
    asyncio.run(run_tests())
