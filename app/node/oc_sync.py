import uuid
from typing import Any
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, ProxyHost, ProxyInbound, inbounds_groups_association
from app.db.models_oc import (
    OCIntegration,
    OCPanel,
    OCPanelConfig,
    OCPanelDestinationHost,
    OCPanelGroup,
    OCUserMapping,
    OCSyncState,
    TenantTelegramConnection,
)
from app.node.user import _bucket_inbounds, _inbounds_from_loaded_groups
from app.services.oc_connection_credentials import panel_has_active_connection
from app.services.oc_user_mapping_state import (
    OC_MAPPING_STATUS_ACTIVE,
    OC_MAPPING_STATUS_DELETED,
    OC_MAPPING_STATUS_PENDING,
    resolve_oc_sync_operation,
)
from app.utils.logger import get_logger

logger = get_logger("oc-sync")


OC_PANEL_STATUS_INACTIVE = "inactive"
OC_PANEL_STATUS_CONNECTION_REVOKED = "connection_revoked"
OC_PANEL_BLOCKING_STATUSES = frozenset({OC_PANEL_STATUS_INACTIVE, OC_PANEL_STATUS_CONNECTION_REVOKED})


async def enqueue_oc_user_sync_many(db_session: AsyncSession, users: list[User]) -> None:
    for db_user in users:
        await enqueue_oc_user_sync(db_session, db_user)


async def tenant_panel_allows_oc_user_mutations(db_session: AsyncSession, panel_id: int) -> bool:
    panel = await db_session.get(OCPanel, panel_id)
    if panel is None:
        return False
    if (panel.sync_status or "") in OC_PANEL_BLOCKING_STATUSES:
        return False
    return await panel_has_active_connection(db_session, panel)


def _user_panel_workspace_compatible(db_user: User, panel: OCPanel) -> bool:
    if panel.workspace_id is None or db_user.workspace_id is None:
        return True
    return db_user.workspace_id == panel.workspace_id


def _delete_sync_payload(
    mapping: OCUserMapping,
    *,
    source_panel_id: str | None,
    integration_id: int | None,
    tenant_id: int | None = None,
    oc_account_id: int | None = None,
    connection_id: int | None = None,
) -> dict[str, Any]:
    """Self-contained OC delete job payload (survives OCPanel / mapping CASCADE)."""
    payload: dict[str, Any] = {"external_user_id": mapping.external_user_id}
    if source_panel_id is not None:
        payload["source_panel_id"] = source_panel_id
    if integration_id is not None:
        payload["integration_id"] = integration_id
    # Snapshot of who may authorise the delete on OC once the OCPanel row is gone.
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    if oc_account_id is not None:
        payload["oc_account_id"] = oc_account_id
    if connection_id is not None:
        payload["connection_id"] = connection_id
    return payload


async def enqueue_mapping_delete_sync(
    db_session: AsyncSession,
    user_id: int,
    panel_id: int,
    mapping: OCUserMapping,
    *,
    connection_id: int | None = None,
) -> None:
    last_configs = mapping.last_synced_configs
    if last_configs is not None:
        last_configs = sorted(last_configs)

    if mapping.status == OC_MAPPING_STATUS_DELETED and last_configs == []:
        return

    entity_id = f"{user_id}_{panel_id}"

    panel_meta = (
        await db_session.execute(
            select(
                OCPanel.source_panel_id,
                OCPanel.integration_id,
                OCPanel.tenant_id,
                OCPanel.oc_account_id,
            ).where(OCPanel.id == panel_id)
        )
    ).one_or_none()
    source_panel_id = panel_meta[0] if panel_meta else None
    integration_id = panel_meta[1] if panel_meta else None
    payload = _delete_sync_payload(
        mapping,
        source_panel_id=source_panel_id,
        integration_id=integration_id,
        tenant_id=panel_meta[2] if panel_meta else None,
        oc_account_id=panel_meta[3] if panel_meta else None,
        connection_id=connection_id,
    )

    pending_job = (
        await db_session.execute(
            select(OCSyncState).where(
                OCSyncState.entity_type == "user_mapping",
                OCSyncState.entity_id == entity_id,
                OCSyncState.status == "pending",
            )
        )
    ).scalars().first()

    if pending_job:
        pending_job.operation = "delete"
        pending_job.payload = payload
        pending_job.revision += 1
        await db_session.flush()
    else:
        job = OCSyncState(
            entity_type="user_mapping",
            entity_id=entity_id,
            operation="delete",
            idempotency_key=f"sync_{user_id}_{panel_id}_{uuid.uuid4()}",
            payload=payload,
            status="pending",
        )
        db_session.add(job)
        await db_session.flush()


async def enqueue_discovery_user_delete_sync(
    db_session: AsyncSession,
    panel: OCPanel,
    *,
    connection_id: int | None = None,
) -> None:
    """Delete the Central integration discovery/test user on the remote OC panel."""
    external_user_id = (panel.test_user_id or "").strip()
    if not external_user_id:
        return

    entity_id = str(panel.id)
    payload = _delete_sync_payload(
        OCUserMapping(
            user_id=0,
            panel_id=panel.id,
            external_user_id=external_user_id,
        ),
        source_panel_id=panel.source_panel_id,
        integration_id=panel.integration_id,
        tenant_id=panel.tenant_id,
        oc_account_id=panel.oc_account_id,
        connection_id=connection_id,
    )

    pending_job = (
        await db_session.execute(
            select(OCSyncState).where(
                OCSyncState.entity_type == "panel_discovery_user",
                OCSyncState.entity_id == entity_id,
                OCSyncState.status == "pending",
            )
        )
    ).scalars().first()
    if pending_job:
        pending_job.operation = "delete"
        pending_job.payload = payload
        pending_job.revision += 1
        await db_session.flush()
        return

    db_session.add(
        OCSyncState(
            entity_type="panel_discovery_user",
            entity_id=entity_id,
            operation="delete",
            idempotency_key=f"discovery_delete_{panel.id}_{uuid.uuid4()}",
            payload=payload,
            status="pending",
        )
    )
    await db_session.flush()


async def enqueue_panel_user_deletions(
    db_session: AsyncSession, panel_id: int, *, connection_id: int | None = None
) -> None:
    mappings = (
        await db_session.execute(select(OCUserMapping).where(OCUserMapping.panel_id == panel_id))
    ).scalars().all()
    for mapping in mappings:
        await enqueue_mapping_delete_sync(
            db_session, mapping.user_id, mapping.panel_id, mapping, connection_id=connection_id
        )


async def remove_imported_panel(db_session: AsyncSession, panel: OCPanel) -> None:
    """
    Remove a PasarGuard panel import. Enqueues OC user delete sync jobs, then
    removes local virtual inbounds/hosts and the OCPanel row (does not delete
    remote Outbound Center infrastructure).
    """
    await enqueue_discovery_user_delete_sync(db_session, panel)
    await enqueue_panel_user_deletions(db_session, panel.id)
    catalog_tags = (
        await db_session.execute(
            select(OCPanelConfig.virtual_inbound_tag).where(OCPanelConfig.panel_id == panel.id)
        )
    ).scalars().all()
    dest_rows = (
        await db_session.execute(
            select(
                OCPanelDestinationHost.virtual_inbound_tag,
                OCPanelDestinationHost.destination_config_id,
            ).where(OCPanelDestinationHost.panel_id == panel.id)
        )
    ).all()
    from app.services.oc_share_link import destination_virtual_inbound_tag

    dest_tags: list[str] = []
    for virt_tag, dest_id in dest_rows:
        if virt_tag:
            dest_tags.append(virt_tag)
        elif dest_id:
            dest_tags.append(destination_virtual_inbound_tag(panel.id, dest_id))
    tags = [t for t in (*catalog_tags, *dest_tags) if t]
    await db_session.delete(panel)
    await db_session.flush()
    if tags:
        inbound_ids = (
            await db_session.execute(select(ProxyInbound.id).where(ProxyInbound.tag.in_(tags)))
        ).scalars().all()
        if inbound_ids:
            await db_session.execute(
                delete(inbounds_groups_association).where(
                    inbounds_groups_association.c.inbound_id.in_(inbound_ids)
                )
            )
        await db_session.execute(delete(ProxyHost).where(ProxyHost.inbound_tag.in_(tags)))
        await db_session.execute(delete(ProxyInbound).where(ProxyInbound.tag.in_(tags)))


async def enqueue_workspace_panel_user_deletions(
    db_session: AsyncSession, workspace_id: int, *, connection_id: int | None = None
) -> None:
    panel_ids = (
        await db_session.execute(select(OCPanel.id).where(OCPanel.workspace_id == workspace_id))
    ).scalars().all()
    if not panel_ids:
        return

    mappings = (
        await db_session.execute(select(OCUserMapping).where(OCUserMapping.panel_id.in_(panel_ids)))
    ).scalars().all()

    for mapping in mappings:
        await enqueue_mapping_delete_sync(
            db_session, mapping.user_id, mapping.panel_id, mapping, connection_id=connection_id
        )


async def enqueue_tenant_panel_user_deletions(
    db_session: AsyncSession, tenant_id: int, *, connection_id: int | None = None
) -> None:
    panel_ids = (
        await db_session.execute(select(OCPanel.id).where(OCPanel.tenant_id == tenant_id))
    ).scalars().all()
    if not panel_ids:
        return

    mappings = (
        await db_session.execute(select(OCUserMapping).where(OCUserMapping.panel_id.in_(panel_ids)))
    ).scalars().all()

    for mapping in mappings:
        await enqueue_mapping_delete_sync(
            db_session, mapping.user_id, mapping.panel_id, mapping, connection_id=connection_id
        )


async def enqueue_oc_connection_revoke(db_session: AsyncSession, connection: TenantTelegramConnection) -> None:
    """Propagate a local revoke to Outbound Center (retried by the sync worker)."""
    if not connection.oc_connection_token_encrypted:
        return  # legacy connection: OC never issued a credential, nothing to revoke there
    from app.services.oc_integration_client import get_active_integration_or_none

    integration = await get_active_integration_or_none(db_session)
    if integration is None:
        return
    entity_id = str(connection.id)
    existing = (
        await db_session.execute(
            select(OCSyncState).where(
                OCSyncState.entity_type == "oc_connection",
                OCSyncState.entity_id == entity_id,
                OCSyncState.status == "pending",
            )
        )
    ).scalars().first()
    if existing:
        return
    db_session.add(
        OCSyncState(
            entity_type="oc_connection",
            entity_id=entity_id,
            operation="revoke",
            idempotency_key=f"oc_connection_revoke_{connection.id}_{uuid.uuid4()}",
            payload={"connection_id": connection.id, "integration_id": integration.id},
            status="pending",
        )
    )
    await db_session.flush()


async def desired_oc_group_ids(
    db_session: AsyncSession, panel_id: int, config_ids: list[str]
) -> list[str]:
    """Real OC group ids behind a user's configs (selected groups only)."""
    if not config_ids:
        return []
    rows = (
        await db_session.execute(
            select(OCPanelGroup.source_group_id)
            .join(OCPanelConfig, OCPanelConfig.panel_group_id == OCPanelGroup.id)
            .where(
                OCPanelConfig.panel_id == panel_id,
                OCPanelConfig.source_config_id.in_(config_ids),
                OCPanelGroup.is_selected.is_(True),
            )
            .distinct()
        )
    ).scalars().all()
    return sorted(set(rows))


async def selected_oc_panel_group_ids(db_session: AsyncSession, panel_id: int) -> list[str]:
    """OC group ids the operator selected during panel import (wizard)."""
    rows = (
        await db_session.execute(
            select(OCPanelGroup.source_group_id).where(
                OCPanelGroup.panel_id == panel_id,
                OCPanelGroup.is_selected.is_(True),
            )
        )
    ).scalars().all()
    return sorted(set(rows))


async def build_oc_user_put_payload(
    db_session: AsyncSession, panel_id: int, config_ids: list[str]
) -> dict[str, list[str]]:
    """
    Payload for OC user PUT.

    When catalog config ids are destination inbound tags, OC often rejects explicit
    ``configs`` unless the user is assigned via ``groups`` first (same as discovery).
    """
    groups = await desired_oc_group_ids(db_session, panel_id, config_ids)
    if not groups:
        groups = await selected_oc_panel_group_ids(db_session, panel_id)
    return {"groups": groups, "configs": config_ids}


async def enqueue_oc_user_sync(db_session: AsyncSession, db_user: User) -> None:
    """
    Enqueue synchronization events (Create, Update, Delete) to Outbound Center
    by diffing the user's active virtual inbounds against their current mappings.
    """
    from app.operation.access_control import check_user_access_allowed

    # Determine user access
    if await check_user_access_allowed(db_user, db_session):
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
        if not await tenant_panel_allows_oc_user_mutations(db_session, panel_id):
            continue

        panel_row = await db_session.get(OCPanel, panel_id)
        if panel_row is not None and not _user_panel_workspace_compatible(db_user, panel_row):
            continue

        mapping = existing_panel_ids.get(panel_id)
        is_new_mapping = False
        if not mapping:
            mapping = OCUserMapping(
                user_id=db_user.id,
                panel_id=panel_id,
                external_user_id=str(uuid.uuid4()),
                status=OC_MAPPING_STATUS_PENDING,
            )
            db_session.add(mapping)
            await db_session.flush() # flush to get external_user_id ready if needed
            existing_panel_ids[panel_id] = mapping
            is_new_mapping = True
        
        # Determine source_config_ids from the virtual inbound tags
        tags = buckets.get(panel_id)
        if tags:
            from app.services.oc_destination_runtime import source_config_ids_for_panel_tags

            configs = await source_config_ids_for_panel_tags(db_session, panel_id, tags)
            if not configs:
                configs = list(
                    (
                        await db_session.execute(
                            select(OCPanelConfig.source_config_id).where(
                                OCPanelConfig.panel_id == panel_id,
                                OCPanelConfig.virtual_inbound_tag.in_(tags),
                                OCPanelConfig.source_missing == False,
                            )
                        )
                    ).scalars().all()
                )
        else:
            configs = []
            
        configs = sorted(configs)
        
        # Phase 10: check drift
        last_configs = mapping.last_synced_configs
        if last_configs is not None:
            last_configs = sorted(last_configs)
            
        needs_sync = (
            is_new_mapping
            or last_configs is None
            or mapping.status != OC_MAPPING_STATUS_ACTIVE
            or last_configs != configs
        )
        if needs_sync:
            if mapping.status != OC_MAPPING_STATUS_ACTIVE:
                mapping.status = OC_MAPPING_STATUS_PENDING
            entity_id = f"{db_user.id}_{panel_id}"
            
            pending_job = (await db_session.execute(
                select(OCSyncState)
                .where(
                    OCSyncState.entity_type == "user_mapping",
                    OCSyncState.entity_id == entity_id,
                    OCSyncState.status == "pending"
                )
            )).scalars().first()

            operation = resolve_oc_sync_operation(
                mapping, is_new_mapping=is_new_mapping, last_configs=last_configs
            )
            payload = await build_oc_user_put_payload(db_session, panel_id, configs)

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
            await enqueue_mapping_delete_sync(db_session, db_user.id, panel_id, mapping)

def oc_panel_status_to_sync_status(oc_status: Any) -> str:
    """OC reports ACTIVE / SUSPENDED / OFFLINE_BY_SYSTEM (any case)."""
    return "connected" if str(oc_status or "").strip().lower() == "active" else OC_PANEL_STATUS_INACTIVE


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


async def sync_panel_from_outbound_center(db_session: AsyncSession, panel_id: int):
    """
    Synchronizes an already imported panel with Outbound Center using the tenant's
    account-scoped connection credential.

    OC is the source of truth for: panel status, groups (id/name) and configs
    (id/name/protocol/network/group mapping). PasarGuard only adds local selection state.
    """
    from sqlalchemy.orm import selectinload

    from app.services.oc_connection_credentials import connection_token_for_panel
    from app.services.oc_integration_client import OcIntegrationApiError, call_oc_api
    from datetime import UTC, datetime as dt

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

    token = await connection_token_for_panel(db_session, panel)
    base = f"/v1/integration/panels/{panel.source_panel_id}"

    # A. Panel metadata sync (readable even when the panel is inactive on OC)
    try:
        panel_data = await call_oc_api(integration, "GET", base, connection_token=token)
    except OcIntegrationApiError as exc:
        if exc.code == "CONNECTION_REVOKED":
            panel.sync_status = OC_PANEL_STATUS_CONNECTION_REVOKED
            await db_session.commit()
        raise
    status_value = oc_panel_status_to_sync_status((panel_data or {}).get("status"))
    panel.sync_status = status_value
    panel.last_sync_at = dt.now(UTC)
    if status_value != "connected":
        # Inactive on OC: keep what we have, stop here. Mappings stay as they are; the
        # worker/enqueue paths skip blocked panels until a later sync sees it ACTIVE again.
        await db_session.commit()
        return

    # B. Group sync
    group_data = await call_oc_api(integration, "GET", f"{base}/groups", connection_token=token)
    oc_groups = group_data.get("groups", [])

    existing_groups = {g.source_group_id: g for g in panel.groups}

    for g_data in oc_groups:
        g_id = str(g_data["id"])
        g_name = str(g_data["name"])

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
    config_data = await call_oc_api(integration, "GET", f"{base}/configs", connection_token=token)
    oc_configs = config_data.get("configs", [])

    existing_configs = {c.source_config_id: c for c in panel.configs}
    oc_config_ids = set()

    from app.services.oc_panel_materialization import upsert_oc_panel_config_row

    existing_group_by_source = existing_groups
    for c_data in oc_configs:
        c_id = str(c_data["id"])
        oc_config_ids.add(c_id)
        await upsert_oc_panel_config_row(
            db_session,
            panel=panel,
            c_data=c_data,
            existing_configs=existing_configs,
            existing_group_by_source=existing_group_by_source,
        )

    for c_id, pc in existing_configs.items():
        if c_id not in oc_config_ids:
            pc.source_missing = True
            tag = pc.virtual_inbound_tag
            if tag:
                host = (
                    await db_session.execute(select(ProxyHost).where(ProxyHost.inbound_tag == tag))
                ).scalar_one_or_none()
                if host is not None:
                    host.is_disabled = True

    from app.services.oc_panel_host_subscription import refresh_panel_hosts_from_discovery_subscription

    if panel.test_user_id:
        try:
            await refresh_panel_hosts_from_discovery_subscription(db_session, panel)
        except Exception:
            logger.exception("oc_discovery_host_refresh_failed panel_id=%s", panel.id)

    from app.services.oc_panel_destination_hosts import ensure_panel_destination_hosts_materialized

    try:
        await ensure_panel_destination_hosts_materialized(db_session, panel)
    except Exception:
        logger.exception("oc_destination_host_repair_failed panel_id=%s", panel.id)

    await db_session.commit()
