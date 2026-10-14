"""Background certificate provisioning and renewal for custom subscription domains."""
from __future__ import annotations

from datetime import UTC, datetime as dt, timedelta as td

from sqlalchemy import select

from app import scheduler
from app.db import GetDB
from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState
from app.services.custom_domain.ssl_lifecycle import (
    certificate_is_operational,
    get_certificate_provisioner,
)
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("custom-domain-ssl")


async def process_custom_domain_certificates() -> None:
    provisioner = get_certificate_provisioner()
    async with GetDB() as db:
        pending = (
            await db.execute(
                select(SubscriptionCustomDomain).where(
                    SubscriptionCustomDomain.state == SubscriptionCustomDomainState.pending_certificate
                )
            )
        ).scalars().all()

        for domain in pending:
            try:
                ref, not_after = await provisioner.issue(domain)
                domain.certificate_ref = ref
                domain.certificate_not_after = not_after
                domain.certificate_last_error = None
                domain.state = SubscriptionCustomDomainState.active
            except Exception as exc:
                domain.state = SubscriptionCustomDomainState.failed
                domain.certificate_last_error = str(exc)[:512]
                logger.warning("Certificate issuance failed for domain id=%s", domain.id)

        renew_cutoff = dt.now(UTC) + td(days=30)
        active = (
            await db.execute(
                select(SubscriptionCustomDomain).where(
                    SubscriptionCustomDomain.state == SubscriptionCustomDomainState.active
                )
            )
        ).scalars().all()
        for domain in active:
            if domain.certificate_not_after is None:
                continue
            not_after = domain.certificate_not_after
            if not_after.tzinfo is None:
                not_after = not_after.replace(tzinfo=UTC)
            if not_after <= renew_cutoff:
                domain.state = SubscriptionCustomDomainState.renewal_pending

        renewing = (
            await db.execute(
                select(SubscriptionCustomDomain).where(
                    SubscriptionCustomDomain.state == SubscriptionCustomDomainState.renewal_pending
                )
            )
        ).scalars().all()
        for domain in renewing:
            try:
                ref, not_after = await provisioner.issue(domain)
                domain.certificate_ref = ref
                domain.certificate_not_after = not_after
                domain.certificate_last_error = None
                domain.state = SubscriptionCustomDomainState.active
            except Exception as exc:
                domain.state = SubscriptionCustomDomainState.failed
                domain.certificate_last_error = str(exc)[:512]

        await db.commit()


if runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        process_custom_domain_certificates,
        "interval",
        seconds=getattr(job_settings, "custom_domain_ssl_interval", 300),
        max_instances=1,
        coalesce=True,
        id="process_custom_domain_certificates",
        replace_existing=True,
    )
