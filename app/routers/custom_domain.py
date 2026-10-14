from fastapi import APIRouter, Depends, Query

from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.custom_domain import (
    CustomDomainCreate,
    CustomDomainCreateResponse,
    CustomDomainIngressStatusResponse,
    CustomDomainResponse,
    CustomDomainVerificationInstructions,
)
from app.operation import OperatorType
from app.operation.custom_domain import CustomDomainOperation
from app.utils import responses

from .authentication import require_permission

router = APIRouter(
    tags=["Custom domains"],
    prefix="/api/custom-domains",
    responses={401: responses._401, 403: responses._403},
)
operator = CustomDomainOperation(operator_type=OperatorType.API)


@router.get("/ingress-status", response_model=CustomDomainIngressStatusResponse)
async def custom_domain_ingress_status(
    admin: AdminDetails = Depends(require_permission("settings", "read")),
):
    return await operator.ingress_status()


@router.get("", response_model=list[CustomDomainResponse])
async def list_custom_domains(
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "read")),
):
    return await operator.list_domains(db, admin)


@router.post("", response_model=CustomDomainCreateResponse, status_code=201)
async def register_custom_domain(
    payload: CustomDomainCreate,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    return await operator.register_domain(db, payload, admin)


@router.get("/{domain_id}", response_model=CustomDomainResponse)
async def get_custom_domain(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "read")),
):
    return await operator.get_domain(db, domain_id, admin)


@router.post("/{domain_id}/verification/instructions", response_model=CustomDomainVerificationInstructions)
async def refresh_verification_instructions(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    return await operator.get_verification_instructions(db, domain_id, admin)


@router.post("/{domain_id}/verification/check", response_model=CustomDomainResponse)
async def check_domain_verification(
    domain_id: int,
    token: str = Query(min_length=8),
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    """Check DNS TXT verification. Pass the plaintext token from registration/instructions as query ``token``."""
    return await operator.check_verification(db, domain_id, admin, token=token)


@router.post("/{domain_id}/certificate/request", response_model=CustomDomainResponse)
async def request_domain_certificate(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    return await operator.request_certificate(db, domain_id, admin)


@router.post("/{domain_id}/disable", response_model=CustomDomainResponse)
async def disable_custom_domain(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    return await operator.disable_domain(db, domain_id, admin)


@router.delete("/{domain_id}", status_code=204)
async def delete_custom_domain(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    await operator.remove_domain(db, domain_id, admin)
