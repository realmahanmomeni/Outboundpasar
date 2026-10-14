"""Revalidate Outbound Center managed subscription subdomains."""
from __future__ import annotations

from datetime import UTC, datetime as dt, timedelta as td

from sqlalchemy import select

from app import scheduler
from app.db import GetDB
from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState, SubscriptionDomainKind
from app.db.models_oc import TenantTelegramConnection, TenantTelegramConnectionStatus
from app.services.custom_domain.managed_subdomain import (
    ManagedSubdomainApiUnavailable,
    ManagedSubdomainNotEligible,
    ManagedSubdomainNotOwned,
)
from app.services.custom_domain.oc_managed_subdomain_client import get_managed_subdomain_client
from app.services.oc_connection_credentials import connection_token_by_id
from app.services.oc_integration_client import get_active_integration_or_none
from app.utils.logger import get_logger
from config import custom_domain_settings, runtime_settings

logger = get_logger("managed-subdomain-revalidate")


async def process_managed_subdomain_revalidation() -> None:
    if not custom_domain_settings.oc_managed_subdomain_api_enabled:
        return
    client = get_managed_subdomain_client()
    async with GetDB() as db:
        integration = await get_active_integration_or_none(db)
        if integration is None:
            return
        rows = (
            await db.execute(
                select(SubscriptionCustomDomain).where(
                    SubscriptionCustomDomain.domain_kind == SubscriptionDomainKind.oc_managed.value,
                    SubscriptionCustomDomain.state.in_(
                        [
                            SubscriptionCustomDomainState.active,
                            SubscriptionCustomDomainState.verified,
                        ]
                    ),
                )
            )
        ).scalars().all()
        cutoff = dt.now(UTC) - td(seconds=custom_domain_settings.oc_managed_subdomain_revalidate_seconds)
        for domain in rows:
            validated = domain.oc_last_validated_at
            if validated is not None:
                if validated.tzinfo is None:
                    validated = validated.replace(tzinfo=UTC)
                if validated > cutoff:
                    continue
            if domain.oc_subdomain_label is None or domain.oc_owner_account_id is None:
                domain.state = SubscriptionCustomDomainState.failed
                domain.certificate_last_error = "managed subdomain metadata incomplete"
                continue
            connection = await db.scalar(
                select(TenantTelegramConnection).where(
                    TenantTelegramConnection.tenant_id == domain.tenant_id,
                    TenantTelegramConnection.oc_account_id == domain.oc_owner_account_id,
                    TenantTelegramConnection.status == TenantTelegramConnectionStatus.active.value,
                    TenantTelegramConnection.active.is_(True),
                    TenantTelegramConnection.revoked_at.is_(None),
                ).limit(1)
            )
            if connection is None:
                domain.state = SubscriptionCustomDomainState.failed
                domain.certificate_last_error = "Outbound Center connection missing for managed subdomain"
                continue
            try:
                token = await connection_token_by_id(db, connection.id)
                record = await client.fetch_owned_subdomain(
                    integration,
                    connection_token=token,
                    label=domain.oc_subdomain_label,
                    expected_account_id=domain.oc_owner_account_id,
                )
            except ManagedSubdomainNotOwned:
                domain.state = SubscriptionCustomDomainState.failed
                domain.certificate_last_error = "managed subdomain no longer owned on Outbound Center"
                continue
            except ManagedSubdomainNotEligible:
                domain.state = SubscriptionCustomDomainState.failed
                domain.certificate_last_error = "managed subdomain suspended on Outbound Center"
                continue
            except ManagedSubdomainApiUnavailable:
                if domain.state != SubscriptionCustomDomainState.active:
                    domain.certificate_last_error = "managed subdomain ownership could not be revalidated"
                continue
            domain.hostname_normalized = record.fqdn
            domain.oc_subdomain_id = record.subdomain_id
            domain.oc_last_validated_at = dt.now(UTC)
            domain.certificate_last_error = None
            if custom_domain_settings.ingress_public_https_ready:
                domain.state = SubscriptionCustomDomainState.active
            else:
                domain.state = SubscriptionCustomDomainState.verified
        await db.commit()


if runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        process_managed_subdomain_revalidation,
        "interval",
        seconds=max(60, custom_domain_settings.oc_managed_subdomain_revalidate_seconds),
        max_instances=1,
        coalesce=True,
        id="process_managed_subdomain_revalidation",
        replace_existing=True,
    )
