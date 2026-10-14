"""Outbound Center bot → PasarGuard managed subdomain bindings (server-to-server)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.db import AsyncSession, get_db
from app.db.crud.managed_subdomain_binding import (
    get_binding_by_label,
    revoke_bot_binding,
    upsert_bot_binding,
)
from app.db.models import ManagedSubdomainBotBindingStatus
from app.models.managed_subdomain_integration import (
    BotManagedSubdomainBindingCreate,
    BotManagedSubdomainBindingResponse,
)
from app.services.custom_domain.managed_subdomain import (
    ManagedSubdomainValidationError,
    fqdn_for_label,
    normalize_managed_subdomain_label,
)
from app.services.integration_request_security import verify_signed_integration_request
from app.services.integration_service_auth import verify_active_integration_token
from app.services.oc_connection_token_lookup import (
    pasarguard_identity_for_connection,
    resolve_active_connection_by_token,
)
from app.utils import responses

router = APIRouter(
    prefix="/api/integration/managed-subdomains",
    tags=["Integration managed subdomains"],
    responses={401: responses._401, 403: responses._403},
)


async def _parse_signed_body(request: Request, db: AsyncSession) -> bytes:
    raw_body = await request.body()
    await verify_signed_integration_request(request, db, raw_body=raw_body)
    return raw_body


@router.post("/bindings", response_model=BotManagedSubdomainBindingResponse)
async def create_managed_subdomain_binding(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_integration_token: str | None = Header(default=None, alias="X-Integration-Token"),
    x_oc_connection_token: str | None = Header(default=None, alias="X-OC-Connection-Token"),
):
    await verify_active_integration_token(db, x_integration_token)
    raw_body = await _parse_signed_body(request, db)
    payload = BotManagedSubdomainBindingCreate.model_validate_json(raw_body)
    if not x_oc_connection_token or not x_oc_connection_token.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-OC-Connection-Token required")

    connection = await resolve_active_connection_by_token(db, x_oc_connection_token)
    tenant_id, workspace_id, binding_admin_id = await pasarguard_identity_for_connection(db, connection)

    try:
        label = normalize_managed_subdomain_label(payload.label)
        fqdn = fqdn_for_label(label)
    except ManagedSubdomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    try:
        row = await upsert_bot_binding(
            db,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            binding_admin_id=binding_admin_id,
            label=label,
            fqdn=fqdn,
            oc_subdomain_id=payload.oc_subdomain_id.strip(),
            oc_account_id=int(connection.oc_account_id),
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

    body = BotManagedSubdomainBindingResponse(
        id=row.id,
        label=row.label,
        fqdn=row.fqdn,
        status=row.status,
        workspace_id=row.workspace_id,
        binding_admin_id=row.binding_admin_id,
    )
    http_status = status.HTTP_200_OK if payload.idempotency_key else status.HTTP_201_CREATED
    return JSONResponse(status_code=http_status, content=body.model_dump())


@router.delete("/bindings/{label}", status_code=204)
async def revoke_managed_subdomain_binding(
    label: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_integration_token: str | None = Header(default=None, alias="X-Integration-Token"),
    x_oc_connection_token: str | None = Header(default=None, alias="X-OC-Connection-Token"),
):
    await verify_active_integration_token(db, x_integration_token)
    await _parse_signed_body(request, db)
    if not x_oc_connection_token or not x_oc_connection_token.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-OC-Connection-Token required")

    connection = await resolve_active_connection_by_token(db, x_oc_connection_token)
    _tenant_id, workspace_id, _binding_admin_id = await pasarguard_identity_for_connection(db, connection)

    try:
        normalized = normalize_managed_subdomain_label(label)
    except ManagedSubdomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    binding = await get_binding_by_label(db, normalized)
    if binding is None or binding.status == ManagedSubdomainBotBindingStatus.revoked.value:
        return None
    if binding.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Subdomain binding belongs to another workspace")

    await revoke_bot_binding(db, binding)
    from app.db.crud.custom_domain import get_managed_domain_for_workspace, set_domain_state
    from app.db.models import SubscriptionCustomDomainState

    domain = await get_managed_domain_for_workspace(db, binding.workspace_id)
    if domain is not None and domain.hostname_normalized == binding.fqdn:
        await set_domain_state(db, domain, SubscriptionCustomDomainState.disabled)
    await db.commit()
    return None
