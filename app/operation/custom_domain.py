from __future__ import annotations

from datetime import UTC, datetime as dt

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.custom_domain import (
    create_custom_domain,
    get_custom_domain_by_id,
    get_managed_domain_for_workspace,
    hash_verification_token,
    list_custom_domains_for_workspace,
    mark_domain_verified,
    refresh_verification_token,
    set_domain_state,
    upsert_managed_subdomain,
)
from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState
from sqlalchemy import select
from app.models.admin import AdminDetails
from app.models.custom_domain import (
    CustomDomainCreate,
    CustomDomainCreateResponse,
    CustomDomainIngressStatusResponse,
    CustomDomainResponse,
    CustomDomainVerificationInstructions,
    ManagedSubdomainAssociate,
)
from app.models.managed_subdomain_integration import (
    ManagedSubdomainSettingsResponse,
    ManagedSubdomainSettingsUpdate,
)
from app.operation import BaseOperation, OperatorType
from app.services.custom_domain.dns import dns_verification_matches
from app.db.crud.managed_subdomain_binding import (
    get_binding_for_claim,
    get_claimed_binding_for_workspace,
    get_pending_binding_for_workspace_admin,
    mark_binding_claimed,
)
from app.services.custom_domain.managed_subdomain import (
    ManagedSubdomainApiUnavailable,
    ManagedSubdomainNotEligible,
    ManagedSubdomainNotOwned,
    ManagedSubdomainValidationError,
    fqdn_for_label,
    managed_domain_suffix,
    normalize_managed_subdomain_input,
    normalize_managed_subdomain_label,
    reject_arbitrary_managed_hostname,
)
from app.services.custom_domain.oc_managed_subdomain_client import (
    get_managed_subdomain_client,
    required_oc_api_contract,
)
from app.services.custom_domain.normalization import (
    DomainValidationError,
    normalize_custom_domain_hostname,
    verification_txt_record_name,
    verification_txt_record_value,
)
from app.services.oc_actor_connection import resolve_telegram_binding_admin_id
from app.services.oc_connection_credentials import (
    OcConnectionTokenMissing,
    connection_token_for_actor,
    get_connection_for_tenant,
)
from app.services.oc_integration_client import get_active_integration
from config import custom_domain_settings
from app.services.custom_domain.ingress_provider import describe_ingress_architecture_options
from app.services.custom_domain.ssl_lifecycle import (
    can_request_certificate,
    get_certificate_provisioner,
    get_ingress_provider_status,
)
from app.services.tenant_admin_scope import require_admin_tenant_id
from app.services.workspace_scope import require_admin_workspace_id


class CustomDomainOperation(BaseOperation):
    def __init__(self, operator_type: OperatorType = OperatorType.API):
        super().__init__(operator_type=operator_type)

    async def _workspace_scope(self, db: AsyncSession, admin: AdminDetails) -> tuple[int, int]:
        tenant_id = await require_admin_tenant_id(db, admin, admin.is_owner)
        workspace_id = await require_admin_workspace_id(db, admin, admin.is_owner)
        return tenant_id, workspace_id

    async def ingress_status(self) -> CustomDomainIngressStatusResponse:
        status = await get_ingress_provider_status()
        return CustomDomainIngressStatusResponse(
            provider=str(status.provider),
            configured=status.configured,
            reachable=status.reachable,
            routing_capable=status.routing_capable,
            public_https_ready=status.public_https_ready,
            can_activate_custom_domains=status.can_activate_custom_domains,
            message=status.message,
            architecture_options=describe_ingress_architecture_options(),
        )

    async def managed_subdomain_api_contract(self) -> dict:
        return required_oc_api_contract()

    async def _binding_admin_id(self, db: AsyncSession, admin: AdminDetails) -> int:
        binding = await resolve_telegram_binding_admin_id(db, admin, admin.is_owner)
        return int(binding if binding is not None else admin.id)

    async def get_managed_subdomain_settings(
        self, db: AsyncSession, admin: AdminDetails
    ) -> ManagedSubdomainSettingsResponse:
        from app.services.custom_domain.serving_readiness import workspace_managed_subdomain_status

        _, workspace_id = await self._workspace_scope(db, admin)
        binding_admin_id = await self._binding_admin_id(db, admin)
        pending = await get_pending_binding_for_workspace_admin(
            db, workspace_id=workspace_id, binding_admin_id=binding_admin_id
        )
        domain = await get_managed_domain_for_workspace(db, workspace_id)
        ownership_linked, serving_ready, label, fqdn = await workspace_managed_subdomain_status(
            db,
            workspace_id=workspace_id,
            binding_admin_id=binding_admin_id,
        )
        state = None
        if domain is not None:
            state = str(domain.state.value if hasattr(domain.state, "value") else domain.state)
        display_label = label if serving_ready else None
        display_fqdn = fqdn if serving_ready else None
        return ManagedSubdomainSettingsResponse(
            domain_suffix=managed_domain_suffix(),
            label=display_label,
            fqdn=display_fqdn,
            pending_claim_label=pending.label if pending else None,
            state=state,
            ownership_linked=ownership_linked,
            serving_ready=serving_ready,
        )

    async def update_managed_subdomain_settings(
        self,
        db: AsyncSession,
        payload: ManagedSubdomainSettingsUpdate,
        admin: AdminDetails,
    ) -> ManagedSubdomainSettingsResponse:
        tenant_id, workspace_id = await self._workspace_scope(db, admin)
        binding_admin_id = await self._binding_admin_id(db, admin)

        if payload.label is None or not str(payload.label).strip():
            domain = await get_managed_domain_for_workspace(db, workspace_id)
            if domain is not None:
                await set_domain_state(db, domain, SubscriptionCustomDomainState.disabled)
                await db.commit()
            return await self.get_managed_subdomain_settings(db, admin)

        try:
            label = normalize_managed_subdomain_label(payload.label)
            fqdn = fqdn_for_label(label)
        except ManagedSubdomainValidationError as exc:
            await self.raise_error(str(exc), 422)

        binding = await get_binding_for_claim(
            db,
            workspace_id=workspace_id,
            binding_admin_id=binding_admin_id,
            label=label,
        )
        if binding is None:
            await self.raise_error(
                "No Outbound Center bot claim exists for this subdomain on your workspace. "
                "Use Connect to central panel in the Telegram bot first.",
                400,
            )

        conflict = (
            await db.execute(
                select(SubscriptionCustomDomain).where(
                    SubscriptionCustomDomain.hostname_normalized == fqdn,
                    SubscriptionCustomDomain.workspace_id != workspace_id,
                )
            )
        ).scalar_one_or_none()
        if conflict is not None:
            await self.raise_error("This subdomain is already associated with another workspace", 409)

        active = custom_domain_settings.ingress_public_https_ready
        try:
            await upsert_managed_subdomain(
                db,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                hostname_normalized=fqdn,
                label=label,
                oc_subdomain_id=binding.oc_subdomain_id,
                oc_owner_account_id=binding.oc_account_id,
                active=active,
            )
            await mark_binding_claimed(db, binding)
            claimed = await get_claimed_binding_for_workspace(db, workspace_id)
            if claimed is not None and claimed.id != binding.id and claimed.label != label:
                from app.db.crud.managed_subdomain_binding import revoke_bot_binding

                await revoke_bot_binding(db, claimed)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            await self.raise_error("This subdomain is already registered", 409)
        return await self.get_managed_subdomain_settings(db, admin)

    async def associate_managed_subdomain(
        self,
        db: AsyncSession,
        payload: ManagedSubdomainAssociate,
        admin: AdminDetails,
    ) -> CustomDomainResponse:
        tenant_id, workspace_id = await self._workspace_scope(db, admin)
        try:
            label, fqdn = normalize_managed_subdomain_input(payload.subdomain)
        except ManagedSubdomainValidationError as exc:
            await self.raise_error(str(exc), 422)

        if not custom_domain_settings.oc_managed_subdomain_api_enabled:
            await self.raise_error(
                "Outbound Center managed subdomain API is not enabled on this deployment",
                503,
            )

        try:
            integration = await get_active_integration(db)
            binding = await resolve_telegram_binding_admin_id(db, admin, admin.is_owner)
            connection = await get_connection_for_tenant(db, tenant_id, binding_admin_id=binding)
            if connection is None or connection.oc_account_id is None:
                await self.raise_error("Connect Telegram to Outbound Center before associating a subdomain", 409)
            token = await connection_token_for_actor(db, admin, admin.is_owner, tenant_id)
            record = await get_managed_subdomain_client().fetch_owned_subdomain(
                integration,
                connection_token=token,
                label=label,
                expected_account_id=int(connection.oc_account_id),
            )
        except OcConnectionTokenMissing:
            await self.raise_error("Outbound Center connection credential missing. Reconnect Telegram.", 409)
        except ManagedSubdomainNotOwned:
            await self.raise_error("This subdomain is not registered to your Outbound Center account", 403)
        except ManagedSubdomainNotEligible:
            await self.raise_error("This subdomain is not eligible for subscription hosting", 409)
        except ManagedSubdomainApiUnavailable:
            await self.raise_error("Could not verify subdomain ownership with Outbound Center", 503)

        conflict = (
            await db.execute(
                select(SubscriptionCustomDomain).where(
                    SubscriptionCustomDomain.hostname_normalized == fqdn,
                    SubscriptionCustomDomain.workspace_id != workspace_id,
                )
            )
        ).scalar_one_or_none()
        if conflict is not None:
            await self.raise_error("This subdomain is already associated with another workspace", 409)

        active = custom_domain_settings.ingress_public_https_ready
        try:
            row = await upsert_managed_subdomain(
                db,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                hostname_normalized=fqdn,
                label=record.label,
                oc_subdomain_id=record.subdomain_id,
                oc_owner_account_id=record.oc_account_id,
                active=active,
            )
            await db.commit()
            await db.refresh(row)
        except IntegrityError:
            await db.rollback()
            await self.raise_error("This subdomain is already registered", 409)
        return CustomDomainResponse.from_db(row)

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
            if custom_domain_settings.custom_domain_register_managed_zone_guard_enabled:
                reject_arbitrary_managed_hostname(hostname)
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
