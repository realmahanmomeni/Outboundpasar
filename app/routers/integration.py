import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.db import AsyncSession, get_db
from app.db.models_oc import (
    OCIntegration,
    OCPanel,
    OCPanelGroup,
    OCPanelConfig,
    TenantTelegramConnection,
)
from app.db.models import ProxyInbound, ProxyHost
from app.models.admin import AdminDetails
from app.routers.panel import get_current_admin_user_context, get_current_user_context
from app.services.integration_tenant import resolve_integration_tenant_id
from app.services.oc_actor_connection import (
    resolve_telegram_binding_admin_id,
    require_oc_telegram_connection_for_actor,
    resolve_oc_telegram_connection_for_actor,
)
from app.services.oc_connection_credentials import (
    call_oc_panel_api,
    connection_token_for_actor,
    encrypt_connection_token,
)
from app.services.oc_integration_client import (
    OcConnectionTokenMissing,
    OcIntegrationApiError,
    call_oc_api,
    get_active_integration,
    http_exception_from_oc_error,
)
from app.services.oc_telegram_connection import (
    activate_connection,
    require_active_telegram_for_tenant_admin,
    revoke_connection,
    set_pending_bot_url,
    start_connection,
    bot_deep_link_for_intent,
)
from app.services.workspace_scope import ensure_actor_panel_access, require_admin_workspace_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integration", tags=["Integration Wizard"])

class PanelItem(BaseModel):
    id: int
    panel_type: str
    name: str
    status: str
    already_imported: bool = False
    imported_panel_id: int | None = None

class AvailablePanelsResponse(BaseModel):
    items: list[PanelItem]


class TelegramConnectionStartResponse(BaseModel):
    intent_id: str
    bot_url: str
    status: str


class TelegramConnectionConfirmRequest(BaseModel):
    code: str = Field(..., min_length=5, max_length=5)


class TelegramConnectionStatusResponse(BaseModel):
    status: str
    active: bool = False
    telegram_user_id: int | None = None
    oc_account_id: int | None = None
    verified_at: str | None = None
    connected_at: str | None = None
    bot_url: str | None = None
    step: str | None = None


class ImportPanelsRequest(BaseModel):
    subscription_ids: list[int] = Field(..., min_length=1)


class ImportPanelResult(BaseModel):
    subscription_id: int
    panel_id: int
    created: bool


class ImportPanelsResponse(BaseModel):
    imported: list[ImportPanelResult]

class SelectPanelRequest(BaseModel):
    source_panel_id: str
    name: str

class SelectPanelResponse(BaseModel):
    panel_id: int
    source_panel_id: str
    test_user_id: str | None

class TestUserResponse(BaseModel):
    test_user_id: str

class GroupItem(BaseModel):
    id: str
    name: str

class GroupListResponse(BaseModel):
    groups: list[GroupItem]

class SyncRequest(BaseModel):
    selected_group_ids: list[str]
    group_names: dict[str, str]

class SyncResponse(BaseModel):
    status: str
    configs_created: int
    hosts_created: int

async def _register_oc_intent(integration: OCIntegration, intent_id: str) -> dict:
    data = await call_oc_api(
        integration,
        "POST",
        "/v1/integration/pasarguard/connection-intents",
        json={"intent_id": intent_id},
    )
    return data if isinstance(data, dict) else {}


# OC machine codes -> persisted panel sync status (so the worker stops hammering OC)
_PANEL_STATUS_BY_OC_CODE = {
    "PANEL_INACTIVE": "inactive",
    "CONNECTION_REVOKED": "connection_revoked",
}


async def _oc_panel_call(
    db: AsyncSession, panel: OCPanel, method: str, path: str, json: dict | None = None
):
    """Account-scoped OC call for a panel with PG-side error mapping + state update."""
    try:
        return await call_oc_panel_api(db, panel, method, path, json)
    except OcIntegrationApiError as e:
        new_status = _PANEL_STATUS_BY_OC_CODE.get(e.code or "")
        if new_status and panel.sync_status != new_status:
            panel.sync_status = new_status
            await db.commit()  # persist before the request fails (get_db rolls back on errors)
        raise http_exception_from_oc_error(e) from e
    except OcConnectionTokenMissing as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e


def _require_tenant_admin(user_context: tuple[str, bool, AdminDetails | None]) -> AdminDetails:
    _identity, is_owner, admin = user_context
    if admin is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return admin


async def _integration_tenant_id(
    db: AsyncSession,
    user_context: tuple[str, bool, AdminDetails | None],
    *,
    auto_provision: bool,
) -> tuple[AdminDetails, int]:
    admin = _require_tenant_admin(user_context)
    _identity, is_owner, _ = user_context
    tenant_id = await resolve_integration_tenant_id(
        db, admin, is_owner, auto_provision=auto_provision
    )
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="Admin is not assigned to a tenant")
    return admin, tenant_id


@router.post("/telegram-connection/start", response_model=TelegramConnectionStartResponse)
async def telegram_connection_start(
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_admin_user_context),
):
    admin, tenant_id = await _integration_tenant_id(db, user_context, auto_provision=True)
    _identity, is_owner, _ = user_context
    binding_admin_id = await resolve_telegram_binding_admin_id(db, admin, is_owner)
    integration = await get_active_integration(db)
    payload = await start_connection(db, tenant_id, binding_admin_id=binding_admin_id)
    try:
        registered = await _register_oc_intent(integration, payload["intent_id"])
    except OcIntegrationApiError as e:
        await db.rollback()
        raise http_exception_from_oc_error(e) from e
    pending_stmt = select(TenantTelegramConnection).where(
        TenantTelegramConnection.tenant_id == tenant_id,
        TenantTelegramConnection.pending_intent_id == payload["intent_id"],
    )
    if binding_admin_id is None:
        pending_stmt = pending_stmt.where(TenantTelegramConnection.binding_admin_id.is_(None))
    else:
        pending_stmt = pending_stmt.where(TenantTelegramConnection.binding_admin_id == binding_admin_id)
    pending = await db.scalar(pending_stmt)
    # Prefer the deep link OC returned; fall back to OC_BOT_USERNAME (503 if neither).
    payload["bot_url"] = set_pending_bot_url(pending, payload["intent_id"], registered.get("bot_url"))
    await db.commit()
    return TelegramConnectionStartResponse(**payload)


@router.post("/telegram-connection/confirm", response_model=TelegramConnectionStatusResponse)
async def telegram_connection_confirm(
    body: TelegramConnectionConfirmRequest,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_admin_user_context),
):
    admin, tenant_id = await _integration_tenant_id(db, user_context, auto_provision=True)
    _identity, is_owner, _ = user_context
    binding_admin_id = await resolve_telegram_binding_admin_id(db, admin, is_owner)

    from app.db.models_oc import TenantTelegramConnection, TenantTelegramConnectionStatus

    pending_stmt = select(TenantTelegramConnection).where(
        TenantTelegramConnection.tenant_id == tenant_id,
        TenantTelegramConnection.status == TenantTelegramConnectionStatus.pending.value,
        TenantTelegramConnection.revoked_at.is_(None),
    )
    if binding_admin_id is None:
        pending_stmt = pending_stmt.where(TenantTelegramConnection.binding_admin_id.is_(None))
    else:
        pending_stmt = pending_stmt.where(TenantTelegramConnection.binding_admin_id == binding_admin_id)
    pending = await db.scalar(pending_stmt)
    if pending is None or not pending.pending_intent_id:
        raise HTTPException(status_code=400, detail="No pending Telegram connection")

    integration = await get_active_integration(db)
    try:
        verify_data = await call_oc_api(
            integration,
            "POST",
            f"/v1/integration/pasarguard/connection-intents/{pending.pending_intent_id}/verify",
            json={"code": body.code.strip()},
        )
    except OcIntegrationApiError as e:
        raise http_exception_from_oc_error(e) from e

    connection_token = str(verify_data.get("connection_token") or "").strip()
    if not connection_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Outbound Center did not issue a connection credential. Upgrade Outbound Center.",
        )
    row = await activate_connection(
        db,
        tenant_id,
        telegram_user_id=int(verify_data["telegram_id"]),
        oc_account_id=int(verify_data["account_id"]),
        connection_token_encrypted=await encrypt_connection_token(connection_token),
        binding_admin_id=binding_admin_id,
    )
    await db.commit()
    return TelegramConnectionStatusResponse(
        status=row.status,
        active=bool(row.active),
        telegram_user_id=row.telegram_user_id,
        oc_account_id=row.oc_account_id,
        verified_at=row.verified_at.isoformat() if row.verified_at else None,
        connected_at=row.connected_at.isoformat() if row.connected_at else None,
        step="connected",
    )


@router.get("/telegram-connection", response_model=TelegramConnectionStatusResponse)
async def telegram_connection_status(
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_user_context),
):
    from app.db.models_oc import TenantTelegramConnection, TenantTelegramConnectionStatus

    admin = _require_tenant_admin(user_context)
    _identity, is_owner, _ = user_context
    tenant_id = await resolve_integration_tenant_id(db, admin, is_owner, auto_provision=False)
    if tenant_id is None:
        return TelegramConnectionStatusResponse(status="unconfigured", step="disconnected")
    active = await resolve_oc_telegram_connection_for_actor(db, admin, is_owner, tenant_id)
    if active is not None:
        return TelegramConnectionStatusResponse(
            status=active.status,
            active=bool(active.active),
            telegram_user_id=active.telegram_user_id,
            oc_account_id=active.oc_account_id,
            verified_at=active.verified_at.isoformat() if active.verified_at else None,
            connected_at=active.connected_at.isoformat() if active.connected_at else None,
            step="connected",
        )

    binding_admin_id = await resolve_telegram_binding_admin_id(db, admin, is_owner)
    pending_stmt = select(TenantTelegramConnection).where(
        TenantTelegramConnection.tenant_id == tenant_id,
        TenantTelegramConnection.status == TenantTelegramConnectionStatus.pending.value,
        TenantTelegramConnection.revoked_at.is_(None),
    )
    if binding_admin_id is None:
        pending_stmt = pending_stmt.where(TenantTelegramConnection.binding_admin_id.is_(None))
    else:
        pending_stmt = pending_stmt.where(TenantTelegramConnection.binding_admin_id == binding_admin_id)
    pending = await db.scalar(pending_stmt)
    if pending is not None and pending.pending_intent_id:
        return TelegramConnectionStatusResponse(
            status="pending",
            active=False,
            bot_url=bot_deep_link_for_intent(pending.pending_intent_id, pending.pending_bot_url),
            step="waiting_for_code",
        )

    return TelegramConnectionStatusResponse(status="none", active=False, step="disconnected")


@router.post("/telegram-connection/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def telegram_connection_revoke(
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_admin_user_context),
):
    admin, tenant_id = await _integration_tenant_id(db, user_context, auto_provision=False)
    _identity, is_owner, _ = user_context
    binding_admin_id = await resolve_telegram_binding_admin_id(db, admin, is_owner)
    await revoke_connection(db, tenant_id, binding_admin_id=binding_admin_id)
    await db.commit()


async def _imported_panels_by_source_id(
    db: AsyncSession,
    integration_id: int,
    tenant_id: int,
    oc_account_id: int,
    *,
    workspace_id: int | None = None,
) -> dict[str, int]:
    stmt = select(OCPanel).where(
        OCPanel.integration_id == integration_id,
        OCPanel.tenant_id == tenant_id,
        OCPanel.oc_account_id == oc_account_id,
    )
    if workspace_id is not None:
        stmt = stmt.where(OCPanel.workspace_id == workspace_id)
    rows = (await db.execute(stmt)).scalars().all()
    return {str(row.source_panel_id): row.id for row in rows}


async def _fetch_account_panels(
    db: AsyncSession,
    integration: OCIntegration,
    tenant_id: int,
    oc_account_id: int,
    admin: AdminDetails,
    is_owner: bool,
) -> list[dict]:
    try:
        token = await connection_token_for_actor(
            db, admin, is_owner, tenant_id, oc_account_id=oc_account_id
        )
        data = await call_oc_api(
            integration,
            "GET",
            f"/v1/integration/accounts/{oc_account_id}/panels",
            connection_token=token,
        )
    except OcIntegrationApiError as e:
        raise http_exception_from_oc_error(e) from e
    except OcConnectionTokenMissing as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    return list(data.get("items") or [])


@router.get("/available-panels", response_model=AvailablePanelsResponse)
async def get_available_panels(
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_user_context),
):
    admin, tenant_id = await _integration_tenant_id(db, user_context, auto_provision=False)

    identity, is_owner, _ = user_context
    connection = await require_oc_telegram_connection_for_actor(db, admin, is_owner, tenant_id)
    workspace_id = None if is_owner else await require_admin_workspace_id(db, admin, is_owner)

    integration = await get_active_integration(db)
    raw_items = await _fetch_account_panels(
        db, integration, tenant_id, connection.oc_account_id, admin, is_owner
    )
    imported = await _imported_panels_by_source_id(
        db, integration.id, tenant_id, connection.oc_account_id, workspace_id=workspace_id
    )
    items = [
        PanelItem(
            id=int(row["subscription_id"]),
            panel_type=str(row["panel_type"]),
            name=str(row["name"]),
            status=str(row["status"]),
            already_imported=str(int(row["subscription_id"])) in imported,
            imported_panel_id=imported.get(str(int(row["subscription_id"]))),
        )
        for row in raw_items
    ]
    return AvailablePanelsResponse(items=items)


@router.post("/import-panels", response_model=ImportPanelsResponse)
async def import_panels(
    body: ImportPanelsRequest,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_admin_user_context),
):
    admin, tenant_id = await _integration_tenant_id(db, user_context, auto_provision=True)

    identity, is_owner, _ = user_context
    connection = await require_oc_telegram_connection_for_actor(db, admin, is_owner, tenant_id)
    workspace_id = None if is_owner else await require_admin_workspace_id(db, admin, is_owner)

    integration = await get_active_integration(db)
    allowed = await _fetch_account_panels(
        db, integration, tenant_id, connection.oc_account_id, admin, is_owner
    )
    allowed_ids = {int(row["subscription_id"]) for row in allowed}
    requested = set(body.subscription_ids)
    if not requested.issubset(allowed_ids):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="One or more panels are not owned by this account")

    imported: list[ImportPanelResult] = []
    oc_account_id = connection.oc_account_id
    already_imported = await _imported_panels_by_source_id(
        db, integration.id, tenant_id, oc_account_id, workspace_id=workspace_id
    )
    for sub_id in body.subscription_ids:
        source_panel_id = str(sub_id)
        if str(sub_id) in already_imported:
            imported.append(
                ImportPanelResult(
                    subscription_id=sub_id,
                    panel_id=already_imported[str(sub_id)],
                    created=False,
                )
            )
            continue

        meta = next((r for r in allowed if int(r["subscription_id"]) == sub_id), None)
        display_name = str(meta["name"]) if meta else f"OC Panel {sub_id}"

        panel_stmt = select(OCPanel).where(
            OCPanel.integration_id == integration.id,
            OCPanel.source_panel_id == source_panel_id,
            OCPanel.tenant_id == tenant_id,
            OCPanel.oc_account_id == oc_account_id,
        )
        if workspace_id is not None:
            panel_stmt = panel_stmt.where(OCPanel.workspace_id == workspace_id)
        existing = (await db.execute(panel_stmt)).scalar_one_or_none()

        created = False
        if existing is None:
            new_panel = OCPanel(
                integration_id=integration.id,
                source_panel_id=source_panel_id,
                purchaser_identity=str(oc_account_id),
                oc_account_id=oc_account_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                name=display_name,
                sync_status="pending",
            )
            db.add(new_panel)
            await db.flush()
            existing = new_panel
            created = True

        imported.append(
            ImportPanelResult(
                subscription_id=sub_id,
                panel_id=existing.id,
                created=created,
            )
        )

    await db.commit()
    return ImportPanelsResponse(imported=imported)

@router.post("/select-panel", response_model=SelectPanelResponse)
async def select_panel(
    req: SelectPanelRequest,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_admin_user_context),
):
    admin, tenant_id = await _integration_tenant_id(db, user_context, auto_provision=True)

    identity, is_owner, _ = user_context
    connection = await require_oc_telegram_connection_for_actor(db, admin, is_owner, tenant_id)
    workspace_id = None if is_owner else await require_admin_workspace_id(db, admin, is_owner)

    try:
        sub_id = int(req.source_panel_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid source_panel_id")

    integration = await get_active_integration(db)
    allowed = await _fetch_account_panels(
        db, integration, tenant_id, connection.oc_account_id, admin, is_owner
    )
    meta = next((r for r in allowed if int(r["subscription_id"]) == sub_id), None)
    if meta is None:
        raise HTTPException(status_code=403, detail="Panel not available for this connected account")

    display_name = req.name or str(meta.get("name") or f"OC Panel {sub_id}")
    panel_stmt = select(OCPanel).where(
        OCPanel.integration_id == integration.id,
        OCPanel.source_panel_id == str(sub_id),
        OCPanel.tenant_id == tenant_id,
        OCPanel.oc_account_id == connection.oc_account_id,
    )
    if workspace_id is not None:
        panel_stmt = panel_stmt.where(OCPanel.workspace_id == workspace_id)
    existing = (await db.execute(panel_stmt)).scalar_one_or_none()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Panel already imported for this account",
                "panel_id": existing.id,
                "source_panel_id": existing.source_panel_id,
            },
        )

    new_panel = OCPanel(
        integration_id=integration.id,
        source_panel_id=str(sub_id),
        purchaser_identity=str(connection.oc_account_id),
        oc_account_id=connection.oc_account_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        name=display_name,
        sync_status="pending",
    )
    db.add(new_panel)
    await db.commit()
    await db.refresh(new_panel)

    return SelectPanelResponse(
        panel_id=new_panel.id,
        source_panel_id=new_panel.source_panel_id,
        test_user_id=new_panel.test_user_id,
    )


@router.post("/panels/{panel_id}/test-user", response_model=TestUserResponse)
async def create_test_user(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_admin_user_context),
):
    identity, is_owner, admin = user_context
    admin = _require_tenant_admin(user_context)

    panel = (
        await db.execute(
            select(OCPanel).options(selectinload(OCPanel.integration)).where(OCPanel.id == panel_id)
        )
    ).scalar_one_or_none()
    await ensure_actor_panel_access(db, panel, is_owner=is_owner, admin=admin)
    await require_active_telegram_for_tenant_admin(db, admin, is_owner)
        
    if panel.test_user_id:
        # Verify it still exists? Or just return it. The prompt says "Reuse existing test user". 
        # We can just call the OC API again because it is idempotent.
        pass
        
    data = await _oc_panel_call(db, panel, "POST", f"/v1/integration/panels/{panel.source_panel_id}/test-user")
    
    panel.test_user_id = data["external_user_id"]
    await db.commit()
    
    return TestUserResponse(test_user_id=panel.test_user_id)

@router.get("/panels/{panel_id}/groups", response_model=GroupListResponse)
async def get_groups(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_user_context),
):
    identity, is_owner, admin = user_context
    admin = _require_tenant_admin(user_context)

    panel = (
        await db.execute(
            select(OCPanel).options(selectinload(OCPanel.integration)).where(OCPanel.id == panel_id)
        )
    ).scalar_one_or_none()
    await ensure_actor_panel_access(db, panel, is_owner=is_owner, admin=admin)
    await require_active_telegram_for_tenant_admin(db, admin, is_owner)
        
    data = await _oc_panel_call(db, panel, "GET", f"/v1/integration/panels/{panel.source_panel_id}/groups")
    return GroupListResponse(groups=data["groups"])

@router.post("/panels/{panel_id}/sync", response_model=SyncResponse)
async def sync_configs_and_hosts(
    panel_id: int,
    req: SyncRequest,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_admin_user_context),
):
    identity, is_owner, admin = user_context
    admin = _require_tenant_admin(user_context)

    panel = (
        await db.execute(
            select(OCPanel)
            .options(
                selectinload(OCPanel.integration),
                selectinload(OCPanel.groups),
                selectinload(OCPanel.configs),
            )
            .where(OCPanel.id == panel_id)
        )
    ).scalar_one_or_none()
    await ensure_actor_panel_access(db, panel, is_owner=is_owner, admin=admin)
    await require_active_telegram_for_tenant_admin(db, admin, is_owner)
        
    # 1. Update groups. OC is the source of truth for which groups exist and their names;
    # the client only chooses which of OC's groups are selected.
    oc_groups = await _oc_panel_call(
        db, panel, "GET", f"/v1/integration/panels/{panel.source_panel_id}/groups"
    )
    oc_group_names = {str(g["id"]): str(g["name"]) for g in oc_groups.get("groups", [])}
    unknown = sorted(set(req.selected_group_ids) - set(oc_group_names))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Groups not available on this panel: {', '.join(unknown)}",
        )
    existing_group_by_source = {g.source_group_id: g for g in panel.groups}
    for source_id, name in oc_group_names.items():
        is_sel = source_id in req.selected_group_ids
        if source_id in existing_group_by_source:
            existing_group_by_source[source_id].is_selected = is_sel
            existing_group_by_source[source_id].source_name = name
        else:
            g = OCPanelGroup(
                panel_id=panel.id,
                source_group_id=source_id,
                source_name=name,
                is_selected=is_sel
            )
            db.add(g)
            panel.groups.append(g)
            existing_group_by_source[source_id] = g
            
    await db.flush()
    
    # 2. Fetch configs
    data = await _oc_panel_call(db, panel, "GET", f"/v1/integration/panels/{panel.source_panel_id}/configs")
    configs_list = data["configs"]
    
    configs_created = 0
    hosts_created = 0
    
    existing_configs = {
        c.source_config_id: c
        for c in (
            await db.execute(select(OCPanelConfig).where(OCPanelConfig.panel_id == panel.id))
        ).scalars()
    }
    
    # Pre-fetch existing inbounds to check uniqueness of tags
    # Tags must be unique in ProxyInbound.
    # Pattern: f"oc_{panel.id}_{config['id']}"
    
    from app.services.oc_panel_materialization import (
        oc_config_import_eligible,
        upsert_oc_panel_config_row,
    )

    for c_data in configs_list:
        mapping = c_data.get("group_mapping", {}) or {}
        if not oc_config_import_eligible(mapping, req.selected_group_ids):
            continue
        _, created_cfg, created_host = await upsert_oc_panel_config_row(
            db,
            panel=panel,
            c_data=c_data,
            existing_configs=existing_configs,
            existing_group_by_source=existing_group_by_source,
        )
        configs_created += created_cfg
        hosts_created += created_host

    from app.services.oc_panel_host_subscription import refresh_panel_hosts_from_discovery_subscription

    if panel.test_user_id:
        try:
            hosts_created += await refresh_panel_hosts_from_discovery_subscription(db, panel)
        except Exception:
            pass

    await db.commit()
    
    # Update panel status
    panel.sync_status = "connected"
    await db.commit()
    
    return SyncResponse(status="success", configs_created=configs_created, hosts_created=hosts_created)
