from __future__ import annotations

from datetime import UTC, datetime as dt

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.custom_domain import (
    create_custom_domain,
    get_custom_domain_by_id,
    hash_verification_token,
    list_custom_domains_for_workspace,
    mark_domain_verified,
    refresh_verification_token,
    set_domain_state,
)
from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState
from app.models.admin import AdminDetails
from app.models.custom_domain import (
    CustomDomainCreate,
    CustomDomainCreateResponse,
    CustomDomainResponse,
    CustomDomainVerificationInstructions,
)
from app.operation import BaseOperation, OperatorType
from app.services.custom_domain.dns import dns_verification_matches
from app.services.custom_domain.normalization import (
    DomainValidationError,
    normalize_custom_domain_hostname,
    verification_txt_record_name,
    verification_txt_record_value,
)
from app.services.custom_domain.ssl_lifecycle import can_request_certificate, get_certificate_provisioner
from app.services.tenant_admin_scope import require_admin_tenant_id
from app.services.workspace_scope import require_admin_workspace_id


class CustomDomainOperation(BaseOperation):
    def __init__(self, operator_type: OperatorType = OperatorType.API):
        super().__init__(operator_type=operator_type)

    async def _workspace_scope(self, db: AsyncSession, admin: AdminDetails) -> tuple[int, int]:
        tenant_id = await require_admin_tenant_id(db, admin, admin.is_owner)
        workspace_id = await require_admin_workspace_id(db, admin, admin.is_owner)
        return tenant_id, workspace_id

    async def list_domains(self, db: AsyncSession, admin: AdminDetails) -> list[CustomDomainResponse]:
        _, workspace_id = await self._workspace_scope(db, admin)
        rows = await list_custom_domains_for_workspace(db, workspace_id)
        return [CustomDomainResponse.from_db(row) for row in rows]

    async def register_domain(
        self,
        db: AsyncSession,
        payload: CustomDomainCreate,
        admin: AdminDetails,
    ) -> CustomDomainCreateResponse:
        tenant_id, workspace_id = await self._workspace_scope(db, admin)
        try:
            hostname = normalize_custom_domain_hostname(payload.hostname)
        except DomainValidationError as exc:
            await self.raise_error(str(exc), 422)

        try:
            row, token = await create_custom_domain(
                db,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                hostname_normalized=hostname,
            )
            await db.commit()
            await db.refresh(row)
        except IntegrityError:
            await db.rollback()
            await self.raise_error("This domain is already registered", 409)

        return CustomDomainCreateResponse(
            **CustomDomainResponse.from_db(row).model_dump(),
            verification=CustomDomainVerificationInstructions(
                record_name=verification_txt_record_name(hostname),
                record_value=verification_txt_record_value(token),
                expires_at=row.verification_expires_at,
            ),
        )

    async def get_domain(self, db: AsyncSession, domain_id: int, admin: AdminDetails) -> CustomDomainResponse:
        _, workspace_id = await self._workspace_scope(db, admin)
        row = await get_custom_domain_by_id(db, domain_id, workspace_id=workspace_id)
        if row is None:
            await self.raise_error("Custom domain not found", 404)
        return CustomDomainResponse.from_db(row)

    async def get_verification_instructions(
        self,
        db: AsyncSession,
        domain_id: int,
        admin: AdminDetails,
    ) -> CustomDomainVerificationInstructions:
        _, workspace_id = await self._workspace_scope(db, admin)
        row = await get_custom_domain_by_id(db, domain_id, workspace_id=workspace_id)
        if row is None:
            await self.raise_error("Custom domain not found", 404)
        if row.state not in {
            SubscriptionCustomDomainState.pending_verification,
            SubscriptionCustomDomainState.failed,
        }:
            await self.raise_error("Domain is not awaiting verification", 409)
        token = await refresh_verification_token(db, row)
        await db.commit()
        return CustomDomainVerificationInstructions(
            record_name=verification_txt_record_name(row.hostname_normalized),
            record_value=verification_txt_record_value(token),
            expires_at=row.verification_expires_at,
        )

    async def check_verification(
        self,
        db: AsyncSession,
        domain_id: int,
        admin: AdminDetails,
        *,
        token: str | None = None,
    ) -> CustomDomainResponse:
        _, workspace_id = await self._workspace_scope(db, admin)
        row = await get_custom_domain_by_id(db, domain_id, workspace_id=workspace_id)
        if row is None:
            await self.raise_error("Custom domain not found", 404)
        if row.state == SubscriptionCustomDomainState.disabled:
            await self.raise_error("Domain is disabled", 409)
        if row.state not in {
            SubscriptionCustomDomainState.pending_verification,
            SubscriptionCustomDomainState.failed,
        }:
            return CustomDomainResponse.from_db(row)

        if row.verification_expires_at is not None:
            expires = row.verification_expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= dt.now(UTC):
                await self.raise_error("Verification token expired; refresh verification instructions", 409)

        if token is None:
            await self.raise_error("Verification token required", 400)

        if row.verification_token_hash != hash_verification_token(token):
            await self.raise_error("Invalid verification token", 400)

        if not await dns_verification_matches(row.hostname_normalized, token):
            await set_domain_state(db, row, SubscriptionCustomDomainState.failed, error="DNS verification failed")
            await db.commit()
            await self.raise_error("DNS verification failed", 400)

        await mark_domain_verified(db, row)
        await db.commit()
        await db.refresh(row)
        return CustomDomainResponse.from_db(row)

    async def request_certificate(
        self,
        db: AsyncSession,
        domain_id: int,
        admin: AdminDetails,
    ) -> CustomDomainResponse:
        _, workspace_id = await self._workspace_scope(db, admin)
        row = await get_custom_domain_by_id(db, domain_id, workspace_id=workspace_id)
        if row is None:
            await self.raise_error("Custom domain not found", 404)
        if not can_request_certificate(row.state):
            await self.raise_error("Certificate cannot be requested in the current state", 409)
        row.certificate_issue_attempts = 0
        row.certificate_issue_locked_until = None
        await set_domain_state(db, row, SubscriptionCustomDomainState.pending_certificate)
        await db.commit()
        await db.refresh(row)
        return CustomDomainResponse.from_db(row)

    async def disable_domain(self, db: AsyncSession, domain_id: int, admin: AdminDetails) -> CustomDomainResponse:
        _, workspace_id = await self._workspace_scope(db, admin)
        row = await get_custom_domain_by_id(db, domain_id, workspace_id=workspace_id)
        if row is None:
            await self.raise_error("Custom domain not found", 404)
        provisioner = get_certificate_provisioner()
        try:
            await provisioner.deprovision(row)
        except Exception:
            pass
        await set_domain_state(db, row, SubscriptionCustomDomainState.disabled)
        await db.commit()
        await db.refresh(row)
        return CustomDomainResponse.from_db(row)

    async def remove_domain(self, db: AsyncSession, domain_id: int, admin: AdminDetails) -> None:
        _, workspace_id = await self._workspace_scope(db, admin)
        row = await get_custom_domain_by_id(db, domain_id, workspace_id=workspace_id)
        if row is None:
            await self.raise_error("Custom domain not found", 404)
        provisioner = get_certificate_provisioner()
        try:
            await provisioner.deprovision(row)
        except Exception:
            pass
        await db.delete(row)
        await db.commit()
