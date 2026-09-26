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
                    OCPanelConfig.virtual_inbound_tag.in_(tags)
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
