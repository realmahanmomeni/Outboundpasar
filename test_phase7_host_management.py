import asyncio
from sqlalchemy import select, delete
from app.db import GetDB
from app.db.models import ProxyHost, ProxyInbound
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelDestinationHost
from app.routers.panel import list_panel_hosts, get_panel_host, update_panel_host, PanelHostUpdate
from fastapi import HTTPException

class MockRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}

async def cleanup_test_data(db):
    await db.execute(delete(OCPanelDestinationHost).where(OCPanelDestinationHost.destination_config_id == "testdest01"))
    await db.execute(delete(OCPanelConfig).where(OCPanelConfig.source_config_id == "101"))
    await db.execute(delete(ProxyHost).where(ProxyHost.inbound_tag.in_(["oc_panel_test_7a_101", "oc_panel_test_7b_101"])))
    await db.execute(delete(ProxyInbound).where(ProxyInbound.tag.in_(["oc_panel_test_7a_101", "oc_panel_test_7b_101"])))
    
    # We must also clean up based on panel.id dynamically if possible, but the best way is to delete anything matching the tags
    await db.execute(delete(ProxyHost).where(ProxyHost.remark.in_(["Original Config Name", "My Custom Name"])))
    await db.execute(delete(ProxyInbound).where(ProxyInbound.tag.like("oc_%_101")))
    
    await db.execute(delete(OCPanel).where(OCPanel.source_panel_id.in_(["panel_test_7a", "panel_test_7b"])))
    await db.commit()

async def run_tests():
    print("Running Phase 7 Tests...")
    async with GetDB() as db:
        await cleanup_test_data(db)
        panel = None
        panel2 = None
        try:
            # Setup
            panel = OCPanel(
                integration_id=1,
                source_panel_id="panel_test_7a",
                purchaser_identity="admin",
                name="Test Panel A"
            )
            db.add(panel)
            await db.flush()
            
            inbound = ProxyInbound(tag=f"oc_{panel.id}_101")
            db.add(inbound)
            await db.flush()
            
            host = ProxyHost(
                remark="Original Config Name",
                priority=0,
                address={"94.140.14.14"},
                port=None,
                path=None,
                status=[],
                alpn=[],
                is_disabled=False,
                allowinsecure=False
            )
            host.inbound = inbound
            db.add(host)
            await db.flush()
            
            config = OCPanelConfig(
                panel_id=panel.id,
                source_config_id="101",
                source_name="Original Config Name",
                virtual_inbound_tag=inbound.tag
            )
            db.add(config)
            db.add(
                OCPanelDestinationHost(
                    panel_id=panel.id,
                    destination_config_id="testdest01",
                    display_name="Original Config Name",
                    virtual_inbound_tag=inbound.tag,
                    source_payload={
                        "subscription_link": "vless://u@94.140.14.14:443?encryption=none#Original%20Config%20Name",
                        "subscription_parsed": {
                            "remark": "Original Config Name",
                            "address": "94.140.14.14",
                            "protocol": "vless",
                        },
                    },
                )
            )
            await db.commit()

            panel2 = OCPanel(
                integration_id=1,
                source_panel_id="panel_test_7b",
                purchaser_identity="admin",
                name="Test Panel B"
            )
            db.add(panel2)
            await db.commit()

            from app.models.admin import AdminDetails, AdminRoleData
            admin_ctx = (
                "admin",
                False,
                AdminDetails(id=1, username="admin", tenant_id=None, role=AdminRoleData(is_owner=True)),
            )

            # 1. Test Host Retrieval (Panel A returns its hosts)
            hosts = await list_panel_hosts(panel.id, db, admin_ctx)
            assert len(hosts) == 1
            assert hosts[0].source_config_name == "testdest01"
            assert hosts[0].display_name == "Original Config Name"
            assert hosts[0].address
            
            host_id = hosts[0].id
            
            # 2. Test Host details
            host_detail = await get_panel_host(panel.id, host_id, db, admin_ctx)
            assert host_detail.id == host_id
            
            # 3. Test Panel isolation
            try:
                await get_panel_host(panel2.id, host_id, db, admin_ctx)
                assert False, "Should have raised 404"
            except HTTPException as e:
                assert e.status_code == 404
                
            try:
                await update_panel_host(panel2.id, host_id, PanelHostUpdate(display_name="Hack"), db, admin_ctx)
                assert False, "Should have raised 404"
            except HTTPException as e:
                assert e.status_code == 404

            # 4. Test Update
            update_data = PanelHostUpdate(
                display_name="My Custom Name",
                is_disabled=True
            )
            updated = await update_panel_host(panel.id, host_id, update_data, db, admin_ctx)
            assert updated.display_name == "My Custom Name"
            assert updated.is_disabled == True
            assert updated.source_config_name == "testdest01"
            
            # Verify in DB
            db_host = (await db.execute(select(ProxyHost).where(ProxyHost.id == host_id))).scalar_one()
            assert db_host.remark == "My Custom Name"
            assert db_host.is_disabled == True
            assert db_host.address

            print("ALL TESTS PASSED.")
        finally:
            # Cleanup only test records
            await cleanup_test_data(db)

if __name__ == "__main__":
    asyncio.run(run_tests())
