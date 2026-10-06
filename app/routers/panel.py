from decimal import Decimal
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload, joinedload

from app.db import AsyncSession, get_db
from app.db.models import ProxyHost, Group
from app.models.admin import AdminDetails
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelDestinationHost, OCPanelGroup
from app.routers.authentication import get_current_for_request, oauth2_scheme
from app.utils.jwt import get_customer_payload
from app.node.oc_sync import remove_imported_panel, sync_panel_from_outbound_center
from app.services.oc_integration_client import (
    OcConnectionTokenMissing,
    OcIntegrationApiError,
    http_exception_from_oc_error,
)
from app.services.oc_actor_connection import resolve_oc_telegram_connection_for_actor
from app.services.oc_telegram_connection import require_active_telegram_for_tenant_admin
from app.services.tenant_admin_scope import require_admin_tenant_id
from app.services.oc_panel_available_configs import (
    available_config_counts_by_panel,
    panel_available_config_count,
)
from app.services.workspace_scope import ensure_actor_panel_access, require_admin_workspace_id

router = APIRouter(prefix="/api/panels", tags=["Panels"])


def _panel_list_stmt():
    return select(OCPanel).options(selectinload(OCPanel.integration)).order_by(OCPanel.id.asc())


def _panel_by_id_stmt(panel_id: int):
    return select(OCPanel).options(selectinload(OCPanel.integration)).where(OCPanel.id == panel_id)


def _panel_response(panel: OCPanel, *, available_configs: int) -> OCPanelResponse:
    return OCPanelResponse(
        id=panel.id,
        integration_id=panel.integration_id,
        name=panel.name,
        source_panel_id=panel.source_panel_id,
        purchaser_identity=panel.purchaser_identity,
        sync_status=panel.sync_status,
        multiplier=float(panel.multiplier) if panel.multiplier is not None else 1.0,
        configs_count=available_configs,
        test_user_id=panel.test_user_id,
        last_sync_at=panel.last_sync_at,
        created_at=panel.created_at,
        updated_at=panel.updated_at,
    )


async def _tenant_oc_panel_filter(
    db: AsyncSession,
    admin: AdminDetails,
    *,
    is_owner: bool,
) -> tuple[int, str, int | None]:
    """
    Imported OC panels are visible only while the tenant Telegram connection is active.

    Returns ``(tenant_id, state, oc_account_id)`` where ``state`` is ``connected`` or ``disconnected``.
    """
    tenant_id = await require_admin_tenant_id(db, admin, is_owner)
    connection = await resolve_oc_telegram_connection_for_actor(db, admin, is_owner, tenant_id)
    if connection is None or connection.oc_account_id is None:
        return tenant_id, "disconnected", None
    return tenant_id, "connected", connection.oc_account_id


class OCPanelResponse(BaseModel):
    id: int
    integration_id: int
    name: str
    source_panel_id: str
    purchaser_identity: str
    sync_status: str | None = None
    multiplier: float = 1.0
    configs_count: int = 0
    test_user_id: str | None = None
    last_sync_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class PanelUpdate(BaseModel):
    multiplier: Decimal | None = Field(default=None, gt=0, decimal_places=2)


class OcHostTechnicalSummary(BaseModel):
    protocol: str | None = None
    network: str | None = None
    address: list[str] = Field(default_factory=list)
    port: int | None = None
    subscription_synced: bool = False


class PanelHostResponse(BaseModel):
    id: int
    display_name: str
    display_name_template: str | None = None
    source_config_name: str
    group_name: str | None
    address: list[str]
    technical: OcHostTechnicalSummary | None = None
    is_disabled: bool
    multiplier: float
    is_oc_imported: bool = True


class PanelHostUpdate(BaseModel):
    display_name: str | None = None
    display_name_template: str | None = None
    multiplier: float | None = None
    is_disabled: bool | None = None

class SyncPanelResponse(BaseModel):
    status: str
    message: str


def _destination_host_response(
    host: ProxyHost,
    dest: OCPanelDestinationHost,
    panel: OCPanel,
    *,
    group_name: str | None = None,
) -> PanelHostResponse:
    payload = dest.source_payload if isinstance(dest.source_payload, dict) else {}
    parsed = payload.get("subscription_parsed") if isinstance(payload.get("subscription_parsed"), dict) else {}
    technical = OcHostTechnicalSummary(
        protocol=parsed.get("protocol"),
        network=parsed.get("network"),
        address=[parsed.get("address")] if parsed.get("address") else list(host.address),
        port=parsed.get("port") or host.port,
        subscription_synced=bool(payload.get("subscription_link")),
    )
    return PanelHostResponse(
        id=host.id,
        display_name=dest.display_name,
        display_name_template=dest.display_name_template,
        source_config_name=dest.destination_config_id,
        group_name=group_name,
        address=technical.address or list(host.address),
        technical=technical,
        is_disabled=bool(host.is_disabled),
        multiplier=float(panel.multiplier),
    )


async def get_current_user_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    token: str | None = Depends(oauth2_scheme),
) -> tuple[str, bool, AdminDetails | None]:
    """
    Returns (identity, is_owner).
    Supports:
    1. Customer JWT: access='customer' -> (account_id, False)
    2. Admin JWT / API key: access='admin'/'sudo' -> (admin.username, admin.is_owner)
       (enforces nodes:read permission for non-owner admins)
    Raises 401 if unauthenticated.
    """
    if token:
        cust_payload = await get_customer_payload(token)
        if cust_payload and "account_id" in cust_payload:
            return (str(cust_payload["account_id"]), False, None)

    try:
        admin = await get_current_for_request(request, db, token)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not admin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    is_owner = admin.is_owner
    if not is_owner:
        from app.operation.permissions import PermissionDenied, enforce_permission

        try:
            enforce_permission(admin, "nodes", "read")
        except PermissionDenied as e:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    return (admin.username, is_owner, admin)


async def get_current_admin_user_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    token: str | None = Depends(oauth2_scheme),
) -> tuple[str, bool, AdminDetails]:
    """
    Returns (identity, is_owner) for mutating operations (PATCH, POST).
    Disallows customer tokens and requires nodes:update permission for non-owner admins.
    """
    if token:
        cust_payload = await get_customer_payload(token)
        if cust_payload and "account_id" in cust_payload:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Customer accounts cannot modify panel settings",
            )

    try:
        admin = await get_current_for_request(request, db, token)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not admin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    is_owner = admin.is_owner
    if not is_owner:
        from app.operation.permissions import PermissionDenied, enforce_permission

        try:
            enforce_permission(admin, "nodes", "update")
        except PermissionDenied as e:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    return (admin.username, is_owner, admin)


@router.get("", response_model=list[OCPanelResponse])
async def list_panels(
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_user_context),
):
    identity, is_owner, admin = user_context
    stmt = _panel_list_stmt()
    if not is_owner:
        if admin is not None:
            tenant_id, oc_state, oc_account_id = await _tenant_oc_panel_filter(db, admin, is_owner=is_owner)
            if oc_state == "disconnected":
                return []
            workspace_id = await require_admin_workspace_id(db, admin, is_owner)
            stmt = stmt.where(
                OCPanel.tenant_id == tenant_id,
                OCPanel.workspace_id == workspace_id,
                OCPanel.oc_account_id == oc_account_id,
            )
        else:
            stmt = stmt.where(OCPanel.purchaser_identity == identity)

    result = await db.execute(stmt)
    panels = result.scalars().all()
    available_counts = await available_config_counts_by_panel(db, panels)

    return [
        _panel_response(p, available_configs=available_counts.get(p.id, 0))
        for p in panels
    ]


@router.get("/{panel_id}", response_model=OCPanelResponse)
async def get_panel(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_user_context),
):
    identity, is_owner, admin = user_context
    stmt = _panel_by_id_stmt(panel_id)
    result = await db.execute(stmt)
    panel = result.scalar_one_or_none()

    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

    if not is_owner:
        await ensure_actor_panel_access(db, panel, is_owner=is_owner, admin=admin, identity=identity)
        if admin is not None and panel.oc_account_id is not None:
            _tenant_id, oc_state, oc_account_id = await _tenant_oc_panel_filter(db, admin, is_owner=is_owner)
            if oc_state == "disconnected" or panel.oc_account_id != oc_account_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

    available = await panel_available_config_count(db, panel)
    return _panel_response(panel, available_configs=available)


@router.delete("/{panel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_panel(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails] = Depends(get_current_admin_user_context),
):
    identity, is_owner, admin = user_context
    stmt = select(OCPanel).where(OCPanel.id == panel_id)
    result = await db.execute(stmt)
    panel = result.scalar_one_or_none()

    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

    if not is_owner:
        await ensure_actor_panel_access(db, panel, is_owner=is_owner, admin=admin, identity=identity)

    await remove_imported_panel(db, panel)
    await db.commit()


@router.patch("/{panel_id}", response_model=OCPanelResponse)
async def update_panel(
    panel_id: int,
    update_data: PanelUpdate,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails] = Depends(get_current_admin_user_context),
):
    identity, is_owner, admin = user_context
    stmt = _panel_by_id_stmt(panel_id)
    result = await db.execute(stmt)
    panel = result.scalar_one_or_none()

    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

    if not is_owner:
        await ensure_actor_panel_access(db, panel, is_owner=is_owner, admin=admin, identity=identity)
        await require_active_telegram_for_tenant_admin(db, admin, is_owner)

    if update_data.multiplier is not None:
        panel.multiplier = float(update_data.multiplier)

    await db.commit()
    await db.refresh(panel, attribute_names=["integration"])

    available = await panel_available_config_count(db, panel)
    return _panel_response(panel, available_configs=available)

@router.post("/{panel_id}/sync", response_model=SyncPanelResponse)
async def sync_panel(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails] = Depends(get_current_admin_user_context),
):
    identity, is_owner, admin = user_context
    panel = (await db.execute(select(OCPanel).where(OCPanel.id == panel_id))).scalar_one_or_none()

    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

    if not is_owner:
        await ensure_actor_panel_access(db, panel, is_owner=is_owner, admin=admin, identity=identity)
        if admin is not None:
            await require_active_telegram_for_tenant_admin(db, admin, is_owner)

    try:
        await sync_panel_from_outbound_center(db, panel_id)
        return SyncPanelResponse(status="success", message="Panel synchronized successfully")
    except OcIntegrationApiError as e:
        raise http_exception_from_oc_error(e) from e
    except OcConnectionTokenMissing as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/{panel_id}/hosts", response_model=list[PanelHostResponse])
async def list_panel_hosts(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_user_context),
):
    identity, is_owner, admin = user_context
    panel = (await db.execute(select(OCPanel).where(OCPanel.id == panel_id))).scalar_one_or_none()
    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

    if not is_owner:
        await ensure_actor_panel_access(db, panel, is_owner=is_owner, admin=admin, identity=identity)
        if admin is not None:
            await require_active_telegram_for_tenant_admin(db, admin, is_owner)

    stmt = (
        select(ProxyHost, OCPanelDestinationHost)
        .join(
            OCPanelDestinationHost,
            ProxyHost.inbound_tag == OCPanelDestinationHost.virtual_inbound_tag,
        )
        .where(
            OCPanelDestinationHost.panel_id == panel_id,
            OCPanelDestinationHost.source_missing.is_(False),
            OCPanelDestinationHost.locally_hidden.is_(False),
        )
        .order_by(OCPanelDestinationHost.display_name.asc())
    )
    result = await db.execute(stmt)
    rows = result.all()

    return [_destination_host_response(host, dest, panel) for host, dest in rows]


@router.get("/{panel_id}/hosts/{host_id}", response_model=PanelHostResponse)
async def get_panel_host(
    panel_id: int,
    host_id: int,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails | None] = Depends(get_current_user_context),
):
    identity, is_owner, admin = user_context
    panel = (await db.execute(select(OCPanel).where(OCPanel.id == panel_id))).scalar_one_or_none()
    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

    if not is_owner:
        await ensure_actor_panel_access(db, panel, is_owner=is_owner, admin=admin, identity=identity)
        if admin is not None:
            await require_active_telegram_for_tenant_admin(db, admin, is_owner)

    stmt = (
        select(ProxyHost, OCPanelDestinationHost)
        .join(
            OCPanelDestinationHost,
            ProxyHost.inbound_tag == OCPanelDestinationHost.virtual_inbound_tag,
        )
        .where(
            OCPanelDestinationHost.panel_id == panel_id,
            ProxyHost.id == host_id,
        )
    )
    result = await db.execute(stmt)
    row = result.first()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Host not found in this panel")

    host, dest = row
    return _destination_host_response(host, dest, panel)


@router.patch("/{panel_id}/hosts/{host_id}", response_model=PanelHostResponse)
async def update_panel_host(
    panel_id: int,
    host_id: int,
    update_data: PanelHostUpdate,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool, AdminDetails] = Depends(get_current_admin_user_context),
):
    identity, is_owner, admin = user_context
    panel = (await db.execute(select(OCPanel).where(OCPanel.id == panel_id))).scalar_one_or_none()
    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

    if not is_owner:
        await ensure_actor_panel_access(db, panel, is_owner=is_owner, admin=admin, identity=identity)
        await require_active_telegram_for_tenant_admin(db, admin, is_owner)

    stmt = (
        select(ProxyHost, OCPanelDestinationHost)
        .join(
            OCPanelDestinationHost,
            ProxyHost.inbound_tag == OCPanelDestinationHost.virtual_inbound_tag,
        )
        .where(
            OCPanelDestinationHost.panel_id == panel_id,
            ProxyHost.id == host_id,
        )
    )
    result = await db.execute(stmt)
    row = result.first()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Host not found in this panel")

    host, dest = row

    if update_data.display_name is not None:
        name = update_data.display_name.strip()
        host.remark = name
        dest.display_name = name
    if update_data.display_name_template is not None:
        dest.display_name_template = update_data.display_name_template
    if update_data.multiplier is not None:
        panel.multiplier = Decimal(str(update_data.multiplier))
    if update_data.is_disabled is not None:
        host.is_disabled = update_data.is_disabled

    await db.commit()
    await db.refresh(host)

    return _destination_host_response(host, dest, panel)
