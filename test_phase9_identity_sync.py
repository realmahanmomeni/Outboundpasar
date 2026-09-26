import asyncio
from sqlalchemy import select

from app.db import GetDB
from app.db.models import User, ProxyInbound, Group, Admin
from app.db.models_oc import OCIntegration, OCPanel, OCPanelGroup, OCPanelConfig, OCUserMapping, OCSyncState
from app.node.user import _bucket_inbounds, _serialize_user_for_node, serialize_user, get_panel_xray_identity
from app.node.oc_sync import enqueue_oc_user_sync

async def test_identity_generation():
    # 123_p92
    assert get_panel_xray_identity(123, 92) == "123_p92"

async def test_bucket_inbounds():
    inbounds = ["native1", "native2", "oc_92_5", "oc_92_6", "oc_93_10"]
    
    buckets = _bucket_inbounds(inbounds)
    assert buckets[None] == ["native1", "native2"]
    assert buckets[92] == ["oc_92_5", "oc_92_6"]
    assert buckets[93] == ["oc_93_10"]

    # Active panels explicitly set
    buckets = _bucket_inbounds(["native1"], active_panel_ids=[92])
    assert buckets[None] == ["native1"]
    assert buckets[92] == []

async def test_serialize_user_for_node():
    user_settings = {"shadowsocks": {"password": "test"}}
    proto_users = _serialize_user_for_node(123, user_settings, ["native1", "oc_92_5"])
    
    assert len(proto_users) == 2
    
    # Check native
    native = next(pu for pu in proto_users if pu.email == "123")
    assert native.inbounds == ["native1"]
    
    # Check panel
    panel = next(pu for pu in proto_users if pu.email == "123_p92")
    assert panel.inbounds == ["oc_92_5"]

    # Check empty active panels
    proto_users = _serialize_user_for_node(123, user_settings, ["native1"], active_panel_ids=[92])
    assert len(proto_users) == 2
    panel = next(pu for pu in proto_users if pu.email == "123_p92")
    assert panel.inbounds == [] # Empty inbounds explicitly generated for removed panels

async def test_enqueue_oc_user_sync(db, test_admin):
    # Create integration
    integration = OCIntegration(base_url="http://test", api_token_encrypted="encrypted_token", token_preview="test")
    db.add(integration)
    await db.flush()

    # Create panel
    panel = OCPanel(integration_id=integration.id, source_panel_id="panel_A", purchaser_identity="test", name="Test")
    db.add(panel)
    await db.flush()

    import uuid
    # Create virtual inbound
    virtual_inbound = ProxyInbound(tag=f"oc_{panel.id}_10_{uuid.uuid4().hex[:8]}")
    db.add(virtual_inbound)
    await db.flush()

    # Create panel group
    panel_group = OCPanelGroup(panel_id=panel.id, source_group_id="group_1", source_name="Group 1", is_selected=True)
    db.add(panel_group)
    await db.flush()

    # Create panel config
    panel_config = OCPanelConfig(panel_id=panel.id, source_config_id="config_10", source_name="Config 10", virtual_inbound_tag=virtual_inbound.tag)
    db.add(panel_config)
    await db.flush()

    # Create native inbound
    native_inbound = ProxyInbound(tag=f"native_1_{uuid.uuid4().hex[:8]}")
    db.add(native_inbound)
    await db.flush()

    # Create local group
    group = Group(name=f"OC Group_{uuid.uuid4().hex[:8]}", inbounds=[virtual_inbound, native_inbound])
    db.add(group)
    await db.flush()
    panel_group.local_group_id = group.id

    # Create User
    user = User(username=f"test_sync_{uuid.uuid4().hex[:8]}", status="active", data_limit=0, admin_id=test_admin.id)
    user.groups = [group]
    db.add(user)
    await db.flush()

    # Run enqueue
    await enqueue_oc_user_sync(db, user)
    await db.commit()

    # Check mappings
    # Mapping is not deleted here in Phase 10. Worker will mark it deleted on success.
    mappings = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == user.id))).scalars().all()
    assert len(mappings) == 1
    assert mappings[0].panel_id == panel.id
    ext_id = mappings[0].external_user_id

    # Check sync states (job)
    jobs = (await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}"))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].operation == "create"
    assert jobs[0].payload["configs"] == ["config_10"]

    # Now remove user from group
    user.groups = []
    await db.flush()
    await enqueue_oc_user_sync(db, user)
    await db.commit()

    # Mapping is not deleted here in Phase 10. Worker will mark it deleted on success.
    mappings = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == user.id))).scalars().all()
    assert len(mappings) == 1

    # Sync state for delete
    jobs = (await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}", OCSyncState.operation == "delete"))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].payload["external_user_id"] == ext_id
    
    return integration, panel, virtual_inbound, panel_group, panel_config, native_inbound, group, user

async def test_enqueue_oc_user_sync_status_disabled(db, test_admin):
    import uuid
    # Test that a disabled user triggers a DELETE sync state
    integration = OCIntegration(base_url="http://test2", api_token_encrypted="encrypted", token_preview="test2")
    db.add(integration)
    await db.flush()

    panel = OCPanel(integration_id=integration.id, source_panel_id="panel_B", purchaser_identity="test", name="Test")
    db.add(panel)
    await db.flush()

    virtual_inbound = ProxyInbound(tag=f"oc_{panel.id}_10_{uuid.uuid4().hex[:8]}")
    db.add(virtual_inbound)
    await db.flush()

    group = Group(name=f"OC Group 2_{uuid.uuid4().hex[:8]}", inbounds=[virtual_inbound])
    db.add(group)
    await db.flush()

    user = User(username=f"test_status_{uuid.uuid4().hex[:8]}", status="active", data_limit=0, admin_id=test_admin.id)
    user.groups = [group]
    db.add(user)
    await db.flush()

    # Initial sync
    await enqueue_oc_user_sync(db, user)
    await db.commit()

    mapping = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == user.id))).scalars().first()
    assert mapping is not None

    # Now change status to disabled
    from app.db.models import UserStatus
    user.status = UserStatus.disabled
    await db.flush()
    await enqueue_oc_user_sync(db, user)
    await db.commit()

    mapping_after = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == user.id))).scalars().first()
    assert mapping_after is not None
    assert mapping_after.external_user_id == mapping.external_user_id

    # Check that a delete job was queued
    jobs = (await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}", OCSyncState.operation == "delete"))).scalars().all()
    assert len(jobs) == 1

    # Reactivate the user
    user.status = UserStatus.active
    await db.flush()
    await enqueue_oc_user_sync(db, user)
    await db.commit()

    mapping_reactivated = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == user.id))).scalars().first()

    # Check that update or create job was queued
    update_jobs = (await db.execute(select(OCSyncState).where(OCSyncState.entity_id == f"{user.id}_{panel.id}", OCSyncState.operation.in_(["create", "update"])))).scalars().all()
    assert len(update_jobs) >= 1
    
    return integration, panel, virtual_inbound, group, user

async def run_tests():
    print("Running Phase 9 Tests...")
    
    await test_identity_generation()
    await test_bucket_inbounds()
    await test_serialize_user_for_node()
    
    async with GetDB() as db:
        test_admin = (await db.execute(select(Admin).limit(1))).scalar_one_or_none()
        if not test_admin:
            print("No admin found in db, skipping tests")
            return
    
    entities_to_delete = []
    
    try:
        async with GetDB() as db:
            e1 = await test_enqueue_oc_user_sync(db, test_admin)
            entities_to_delete.extend(e1)
            print("   [x] enqueue_oc_user_sync mapping lifecycle OK")

            e2 = await test_enqueue_oc_user_sync_status_disabled(db, test_admin)
            entities_to_delete.extend(e2)
            print("   [x] enqueue_oc_user_sync_status_disabled mapping lifecycle OK")
            
        print("\nALL PHASE 9 TESTS PASSED SUCCESSFULLY!")
    finally:
        async with GetDB() as db:
            for entity in entities_to_delete:
                try:
                    await db.delete(entity)
                except Exception:
                    pass
            await db.commit()

if __name__ == "__main__":
    asyncio.run(run_tests())
