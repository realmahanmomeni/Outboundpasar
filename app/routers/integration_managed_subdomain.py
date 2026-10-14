"""Outbound Center bot → PasarGuard managed subdomain bindings (server-to-server)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.db import AsyncSession, get_db
from app.db.crud.managed_subdomain_binding import (
    get_binding_by_label,
    revoke_bot_binding,
    upsert_bot_binding,
    workspace_belongs_to_tenant,
)
from app.db.models import Admin, ManagedSubdomainBotBindingStatus
from app.models.managed_subdomain_integration import (
    BotManagedSubdomainBindingCreate,
    BotManagedSubdomainBindingResponse,
)
from app.services.custom_domain.managed_subdomain import (
    ManagedSubdomainValidationError,
    fqdn_for_label,
    normalize_managed_subdomain_label,
)
from app.services.integration_service_auth import verify_active_integration_token
from app.utils import responses

router = APIRouter(
    prefix="/api/integration/managed-subdomains",
    tags=["Integration managed subdomains"],
    responses={401: responses._401, 403: responses._403},
)


async def _integration_auth(
    db: AsyncSession = Depends(get_db),
    x_integration_token: str | None = Header(default=None, alias="X-Integration-Token"),
):
    return await verify_active_integration_token(db, x_integration_token)


@router.post("/bindings", response_model=BotManagedSubdomainBindingResponse, status_code=201)
async def create_managed_subdomain_binding(
    payload: BotManagedSubdomainBindingCreate,
    db: AsyncSession = Depends(get_db),
    _integration=Depends(_integration_auth),
):
    """
    Idempotent: Outbound Center bot registers a subdomain for a workspace/admin before the panel admin
    confirms it in Settings.
    """
    try:
        label = normalize_managed_subdomain_label(payload.label)
        fqdn = fqdn_for_label(label)
    except ManagedSubdomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if not await workspace_belongs_to_tenant(db, payload.workspace_id, payload.tenant_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Workspace does not belong to tenant")

    admin = await db.get(Admin, payload.binding_admin_id)
    if admin is None or admin.tenant_id != payload.tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid binding admin for tenant")

    try:
        row = await upsert_bot_binding(
            db,
            tenant_id=payload.tenant_id,
            workspace_id=payload.workspace_id,
            binding_admin_id=payload.binding_admin_id,
            label=label,
            fqdn=fqdn,
            oc_subdomain_id=payload.oc_subdomain_id.strip(),
            oc_account_id=payload.oc_account_id,
            idempotency_key=payload.idempotency_key,
        )
        await db.commit()
        await db.refresh(row)
    except ValueError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Subdomain label is already registered for another workspace",
        )

    return BotManagedSubdomainBindingResponse(
        id=row.id,
        label=row.label,
        fqdn=row.fqdn,
        status=row.status,
        workspace_id=row.workspace_id,
        binding_admin_id=row.binding_admin_id,
    )


@router.delete("/bindings/{label}", status_code=204)
async def revoke_managed_subdomain_binding(
    label: str,
    db: AsyncSession = Depends(get_db),
    _integration=Depends(_integration_auth),
):
    try:
        normalized = normalize_managed_subdomain_label(label)
    except ManagedSubdomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    binding = await get_binding_by_label(db, normalized)
    if binding is None or binding.status == ManagedSubdomainBotBindingStatus.revoked.value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Binding not found")

    await revoke_bot_binding(db, binding)
    from app.db.crud.custom_domain import get_managed_domain_for_workspace, set_domain_state
    from app.db.models import SubscriptionCustomDomainState

    domain = await get_managed_domain_for_workspace(db, binding.workspace_id)
    if domain is not None and domain.hostname_normalized == binding.fqdn:
        await set_domain_state(db, domain, SubscriptionCustomDomainState.disabled)
    await db.commit()
