import uuid
from typing import Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.models_oc import OCPanelConfig, OCUserMapping, OCSyncState
from app.node.user import _bucket_inbounds, _inbounds_from_loaded_groups

async def enqueue_oc_user_sync(db_session: AsyncSession, db_user: User) -> None:
    """
    Enqueue synchronization events (Create, Update, Delete) to Outbound Center
    by diffing the user's active virtual inbounds against their current mappings.
    """
    from app.db.models import UserStatus

    # Determine user status
    status = db_user.__dict__.get("status")
    if status is None:
        status = await db_user.awaitable_attrs.status

    if status in (UserStatus.active, UserStatus.on_hold):
        inbounds = _inbounds_from_loaded_groups(db_user)
        if inbounds is None:
            inbounds = await db_user.inbounds()
        
        buckets = _bucket_inbounds(inbounds)
        active_panel_ids = {p for p in buckets.keys() if p is not None and buckets[p]}
    else:
        active_panel_ids = set()
        buckets = {}

    # Load existing mappings
    existing_mappings = (await db_session.execute(
        select(OCUserMapping).where(OCUserMapping.user_id == db_user.id)
    )).scalars().all()
    
    existing_panel_ids = {m.panel_id: m for m in existing_mappings}

    for panel_id in active_panel_ids:
        mapping = existing_panel_ids.get(panel_id)
        is_new_mapping = False
        if not mapping:
            mapping = OCUserMapping(
                user_id=db_user.id,
                panel_id=panel_id,
                external_user_id=str(uuid.uuid4()),
                status="active"
            )
            db_session.add(mapping)
            await db_session.flush() # flush to get external_user_id ready if needed
            existing_panel_ids[panel_id] = mapping
            is_new_mapping = True
        
        # Determine source_config_ids from the virtual inbound tags
        tags = buckets.get(panel_id)
        if tags:
            configs = (await db_session.execute(
                select(OCPanelConfig.source_config_id)
                .where(
                    OCPanelConfig.panel_id == panel_id,
                    OCPanelConfig.virtual_inbound_tag.in_(tags),
                    OCPanelConfig.source_missing == False
                )
            )).scalars().all()
        else:
            configs = []
            
        configs = sorted(configs)
        
        # Phase 10: check drift
        last_configs = mapping.last_synced_configs
        if last_configs is not None:
            last_configs = sorted(last_configs)
            
        if is_new_mapping or last_configs is None or mapping.status != "active" or last_configs != configs:
            entity_id = f"{db_user.id}_{panel_id}"
            
            pending_job = (await db_session.execute(
                select(OCSyncState)
                .where(
                    OCSyncState.entity_type == "user_mapping",
                    OCSyncState.entity_id == entity_id,
                    OCSyncState.status == "pending"
                )
            )).scalars().first()

            operation = "update" if (not is_new_mapping and last_configs is not None) else "create"
            payload = {"groups": [], "configs": configs}

            if pending_job:
                pending_job.operation = operation
                pending_job.payload = payload
                pending_job.revision += 1
            else:
                job = OCSyncState(
                    entity_type="user_mapping",
                    entity_id=entity_id,
                    operation=operation,
                    idempotency_key=f"sync_{db_user.id}_{panel_id}_{uuid.uuid4()}",
                    payload=payload,
                    status="pending"
                )
                db_session.add(job)
        
    for panel_id, mapping in existing_panel_ids.items():
        if panel_id not in active_panel_ids:
            last_configs = mapping.last_synced_configs
            if last_configs is not None:
                last_configs = sorted(last_configs)

            if mapping.status != "deleted" or last_configs != []:
                entity_id = f"{db_user.id}_{panel_id}"
                
                pending_job = (await db_session.execute(
                    select(OCSyncState)
                    .where(
                        OCSyncState.entity_type == "user_mapping",
                        OCSyncState.entity_id == entity_id,
                        OCSyncState.status == "pending"
                    )
                )).scalars().first()

                if pending_job:
                    pending_job.operation = "delete"
                    pending_job.payload = {"external_user_id": mapping.external_user_id}
                    pending_job.revision += 1
                else:
                    job = OCSyncState(
                        entity_type="user_mapping",
                        entity_id=entity_id,
                        operation="delete",
                        idempotency_key=f"sync_{db_user.id}_{panel_id}_{uuid.uuid4()}",
                        payload={"external_user_id": mapping.external_user_id},
                        status="pending"
                    )
                    db_session.add(job)

async def sync_panel_from_outbound_center(db_session: AsyncSession, panel_id: int):
    """
    Synchronizes an already imported panel with Outbound Center.
    Handles metadata, groups, and configs updates securely.
    """
    from sqlalchemy.orm import selectinload
    from app.db.models_oc import OCPanel, OCPanelGroup, OCPanelConfig
    from app.db.models import ProxyInbound, ProxyHost
    from app.routers.integration import call_oc_api

    panel = (await db_session.execute(
        select(OCPanel)
        .options(
            selectinload(OCPanel.integration),
            selectinload(OCPanel.groups),
            selectinload(OCPanel.configs)
        )
        .where(OCPanel.id == panel_id)
        .with_for_update()
    )).scalar_one_or_none()

    if not panel:
        raise ValueError(f"Panel {panel_id} not found")
        
    integration = panel.integration
    if not integration or not integration.is_active:
        raise ValueError(f"Integration not active for panel {panel_id}")

    # A. Panel metadata sync
    panel_data = await call_oc_api(integration, "GET", f"/v1/integration/panels/{panel.source_panel_id}")
    if panel_data:
        panel.sync_status = "connected" if panel_data.get("status") == "active" else panel_data.get("status")

    # B. Group sync
    group_data = await call_oc_api(integration, "GET", f"/v1/integration/panels/{panel.source_panel_id}/groups")
    oc_groups = group_data.get("groups", [])
    
    existing_groups = {g.source_group_id: g for g in panel.groups}
    oc_group_ids = set()
    
    for g_data in oc_groups:
        g_id = g_data["id"]
        g_name = g_data["name"]
        oc_group_ids.add(g_id)
        
        if g_id in existing_groups:
            existing_groups[g_id].source_name = g_name
        else:
            new_group = OCPanelGroup(
                panel_id=panel.id,
                source_group_id=g_id,
                source_name=g_name,
                is_selected=False  # Safe default for new groups
            )
            db_session.add(new_group)
            panel.groups.append(new_group)
            existing_groups[g_id] = new_group

    await db_session.flush()

    # C. Config sync
    config_data = await call_oc_api(integration, "GET", f"/v1/integration/panels/{panel.source_panel_id}/configs")
    oc_configs = config_data.get("configs", [])
    
    existing_configs = {c.source_config_id: c for c in panel.configs}
    oc_config_ids = set()
    
    for c_data in oc_configs:
        c_id = c_data["id"]
        c_name = c_data["name"]
        oc_config_ids.add(c_id)
        
        mapping = c_data.get("group_mapping", {})
        is_supported = mapping.get("supported", False)
        mapped_groups = mapping.get("groups", [])
        
        local_group_id = None
        if is_supported and mapped_groups:
            for g_id in mapped_groups:
                if g_id in existing_groups:
                    local_group_id = existing_groups[g_id].id
                    break
        
        tag = f"oc_{panel.id}_{c_id}"
        
        if c_id in existing_configs:
            pc = existing_configs[c_id]
            old_source_name = pc.source_name
            pc.source_name = c_name
            pc.panel_group_id = local_group_id
            pc.source_missing = False
            
            host = (await db_session.execute(select(ProxyHost).where(ProxyHost.inbound_tag == tag))).scalar_one_or_none()
            if host and host.remark == old_source_name and host.remark != c_name:
                host.remark = c_name
        else:
            inbound = (await db_session.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one_or_none()
            if not inbound:
                inbound = ProxyInbound(tag=tag)
                db_session.add(inbound)
                await db_session.flush()
                
            host = (await db_session.execute(select(ProxyHost).where(ProxyHost.inbound_tag == tag))).scalar_one_or_none()
            if not host:
                host = ProxyHost(
                    remark=c_name,
                    priority=0,
                    address={"8.8.8.8"},
                    port=None,
                    path=None,
                    allowinsecure=None,
                    alpn=[],
                    status=[]
                )
                host.inbound = inbound
                db_session.add(host)
                
            pc = OCPanelConfig(
                panel_id=panel.id,
                source_config_id=c_id,
                source_name=c_name,
                panel_group_id=local_group_id,
                virtual_inbound_tag=tag,
                source_missing=False
            )
            db_session.add(pc)
            panel.configs.append(pc)
            existing_configs[c_id] = pc
            
    for c_id, pc in existing_configs.items():
        if c_id not in oc_config_ids:
            pc.source_missing = True

    await db_session.commit()

