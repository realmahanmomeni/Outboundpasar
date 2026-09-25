from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import selectinload, joinedload

from app.db import AsyncSession, get_db
from app.db.models import ProxyHost, Group
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelGroup
from app.routers.authentication import get_current_for_request, oauth2_scheme
from app.utils.jwt import get_customer_payload

router = APIRouter(prefix="/api/panels", tags=["Panels"])


class OCPanelResponse(BaseModel):
    id: int
    integration_id: int
    name: str
    source_panel_id: str
    purchaser_identity: str
    sync_status: str | None = None
    default_multiplier: float = 1.0
    configs_count: int = 0
    test_user_id: str | None = None
    last_sync_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class PanelHostResponse(BaseModel):
    id: int
    display_name: str
    source_config_name: str
    group_name: str | None
    address: list[str]
    is_disabled: bool
    multiplier: float | None


class PanelHostUpdate(BaseModel):
    display_name: str | None = None
    is_disabled: bool | None = None
    multiplier: float | None = None


async def get_current_user_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    token: str | None = Depends(oauth2_scheme),
) -> tuple[str, bool]:
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
            return (str(cust_payload["account_id"]), False)

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

    return (admin.username, is_owner)


@router.get("", response_model=list[OCPanelResponse])
async def list_panels(
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool] = Depends(get_current_user_context),
):
    identity, is_owner = user_context
    stmt = select(OCPanel).options(selectinload(OCPanel.configs)).order_by(OCPanel.id.asc())
    if not is_owner:
        stmt = stmt.where(OCPanel.purchaser_identity == identity)

    result = await db.execute(stmt)
    panels = result.scalars().all()

    return [
        OCPanelResponse(
            id=p.id,
            integration_id=p.integration_id,
            name=p.name,
            source_panel_id=p.source_panel_id,
            purchaser_identity=p.purchaser_identity,
            sync_status=p.sync_status,
            default_multiplier=float(p.default_multiplier) if p.default_multiplier is not None else 1.0,
            configs_count=len(p.configs) if p.configs else 0,
            test_user_id=p.test_user_id,
            last_sync_at=p.last_sync_at,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in panels
    ]


@router.get("/{panel_id}", response_model=OCPanelResponse)
async def get_panel(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool] = Depends(get_current_user_context),
):
    identity, is_owner = user_context
    stmt = select(OCPanel).options(selectinload(OCPanel.configs)).where(OCPanel.id == panel_id)
    result = await db.execute(stmt)
    panel = result.scalar_one_or_none()

    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")

    if not is_owner and panel.purchaser_identity != identity:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this panel")

    return OCPanelResponse(
        id=panel.id,
        integration_id=panel.integration_id,
        name=panel.name,
        source_panel_id=panel.source_panel_id,
        purchaser_identity=panel.purchaser_identity,
        sync_status=panel.sync_status,
        default_multiplier=float(panel.default_multiplier) if panel.default_multiplier is not None else 1.0,
        configs_count=len(panel.configs) if panel.configs else 0,
        test_user_id=panel.test_user_id,
        last_sync_at=panel.last_sync_at,
        created_at=panel.created_at,
        updated_at=panel.updated_at,
    )


@router.get("/{panel_id}/hosts", response_model=list[PanelHostResponse])
async def list_panel_hosts(
    panel_id: int,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool] = Depends(get_current_user_context),
):
    identity, is_owner = user_context
    panel = (await db.execute(select(OCPanel).where(OCPanel.id == panel_id))).scalar_one_or_none()
    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")
    if not is_owner and panel.purchaser_identity != identity:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    stmt = (
        select(ProxyHost, OCPanelConfig, OCPanelGroup, Group)
        .join(OCPanelConfig, ProxyHost.inbound_tag == OCPanelConfig.virtual_inbound_tag)
        .outerjoin(OCPanelGroup, OCPanelConfig.panel_group_id == OCPanelGroup.id)
        .outerjoin(Group, OCPanelGroup.local_group_id == Group.id)
        .where(OCPanelConfig.panel_id == panel_id)
        .order_by(ProxyHost.id.asc())
    )
    result = await db.execute(stmt)
    rows = result.all()

    hosts = []
    for host, config, oc_group, local_group in rows:
        group_name = local_group.name if local_group else (oc_group.source_name if oc_group else None)
        hosts.append(PanelHostResponse(
            id=host.id,
            display_name=host.remark,
            source_config_name=config.source_name,
            group_name=group_name,
            address=list(host.address),
            is_disabled=bool(host.is_disabled),
            multiplier=float(host.multiplier_override) if host.multiplier_override is not None else None,
        ))
    return hosts


@router.get("/{panel_id}/hosts/{host_id}", response_model=PanelHostResponse)
async def get_panel_host(
    panel_id: int,
    host_id: int,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool] = Depends(get_current_user_context),
):
    identity, is_owner = user_context
    panel = (await db.execute(select(OCPanel).where(OCPanel.id == panel_id))).scalar_one_or_none()
    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")
    if not is_owner and panel.purchaser_identity != identity:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    stmt = (
        select(ProxyHost, OCPanelConfig, OCPanelGroup, Group)
        .join(OCPanelConfig, ProxyHost.inbound_tag == OCPanelConfig.virtual_inbound_tag)
        .outerjoin(OCPanelGroup, OCPanelConfig.panel_group_id == OCPanelGroup.id)
        .outerjoin(Group, OCPanelGroup.local_group_id == Group.id)
        .where(OCPanelConfig.panel_id == panel_id)
        .where(ProxyHost.id == host_id)
    )
    result = await db.execute(stmt)
    row = result.first()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Host not found in this panel")

    host, config, oc_group, local_group = row
    group_name = local_group.name if local_group else (oc_group.source_name if oc_group else None)
    
    return PanelHostResponse(
        id=host.id,
        display_name=host.remark,
        source_config_name=config.source_name,
        group_name=group_name,
        address=list(host.address),
        is_disabled=bool(host.is_disabled),
        multiplier=float(host.multiplier_override) if host.multiplier_override is not None else None,
    )


@router.patch("/{panel_id}/hosts/{host_id}", response_model=PanelHostResponse)
async def update_panel_host(
    panel_id: int,
    host_id: int,
    update_data: PanelHostUpdate,
    db: AsyncSession = Depends(get_db),
    user_context: tuple[str, bool] = Depends(get_current_user_context),
):
    identity, is_owner = user_context
    panel = (await db.execute(select(OCPanel).where(OCPanel.id == panel_id))).scalar_one_or_none()
    if not panel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found")
    if not is_owner and panel.purchaser_identity != identity:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    stmt = (
        select(ProxyHost, OCPanelConfig, OCPanelGroup, Group)
        .join(OCPanelConfig, ProxyHost.inbound_tag == OCPanelConfig.virtual_inbound_tag)
        .outerjoin(OCPanelGroup, OCPanelConfig.panel_group_id == OCPanelGroup.id)
        .outerjoin(Group, OCPanelGroup.local_group_id == Group.id)
        .where(OCPanelConfig.panel_id == panel_id)
        .where(ProxyHost.id == host_id)
    )
    result = await db.execute(stmt)
    row = result.first()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Host not found in this panel")

    host, config, oc_group, local_group = row

    if update_data.display_name is not None:
        host.remark = update_data.display_name
    if update_data.is_disabled is not None:
        host.is_disabled = update_data.is_disabled
    if update_data.multiplier is not None:
        host.multiplier_override = update_data.multiplier

    await db.commit()
    await db.refresh(host)

    group_name = local_group.name if local_group else (oc_group.source_name if oc_group else None)
    
    return PanelHostResponse(
        id=host.id,
        display_name=host.remark,
        source_config_name=config.source_name,
        group_name=group_name,
        address=list(host.address),
        is_disabled=bool(host.is_disabled),
        multiplier=float(host.multiplier_override) if host.multiplier_override is not None else None,
    )
