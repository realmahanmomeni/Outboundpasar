"""Background certificate provisioning and renewal for custom subscription domains."""
from __future__ import annotations

from datetime import UTC, datetime as dt, timedelta as td

from sqlalchemy import or_, select

from app import scheduler
from app.db import GetDB
from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState, SubscriptionDomainKind
from app.services.custom_domain import bootstrap as _custom_domain_bootstrap  # noqa: F401
from app.services.custom_domain.ssl_lifecycle import (
    CertificateProvisionerError,
    CertificateRateLimited,
    CertificateProviderUnavailable,
    certificate_is_operational,
    domain_eligible_for_certificate_job,
    domain_is_routable_for_subscriptions,
    get_certificate_provisioner,
    ingress_ready_for_hostname,
    sanitize_certificate_error,
)
from app.utils.logger import get_logger
from config import custom_domain_settings, job_settings, runtime_settings

logger = get_logger("custom-domain-ssl")


def _retry_delay_seconds() -> int:
    return max(60, custom_domain_settings.certificate_retry_backoff_seconds)


async def _process_issuance_row(domain: SubscriptionCustomDomain, provisioner) -> None:
    if domain.state == SubscriptionCustomDomainState.disabled:
        return
    if domain.domain_kind == SubscriptionDomainKind.oc_managed.value:
        return
    if not domain_eligible_for_certificate_job(domain):
        domain.state = SubscriptionCustomDomainState.failed
        domain.certificate_last_error = "domain is not eligible for certificate issuance"
        return

    now = dt.now(UTC)
    locked_until = domain.certificate_issue_locked_until
    if locked_until is not None:
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=UTC)
        if locked_until > now:
            return

    try:
        ref, not_after = await provisioner.issue(domain)
    except CertificateRateLimited as exc:
        domain.certificate_issue_locked_until = now + td(seconds=_retry_delay_seconds())
        domain.certificate_last_error = sanitize_certificate_error(exc)
        return
    except CertificateProviderUnavailable as exc:
        domain.certificate_issue_attempts += 1
        domain.certificate_issue_locked_until = now + td(seconds=_retry_delay_seconds())
        domain.certificate_last_error = sanitize_certificate_error(exc)
        if domain.certificate_issue_attempts >= custom_domain_settings.certificate_max_attempts:
            domain.state = SubscriptionCustomDomainState.failed
        return
    except CertificateProvisionerError as exc:
        domain.certificate_issue_attempts += 1
        domain.certificate_last_error = sanitize_certificate_error(exc)
        if domain.certificate_issue_attempts >= custom_domain_settings.certificate_max_attempts:
            domain.state = SubscriptionCustomDomainState.failed
        else:
            domain.certificate_issue_locked_until = now + td(seconds=_retry_delay_seconds())
        return
    except Exception as exc:
        domain.certificate_issue_attempts += 1
        domain.certificate_last_error = sanitize_certificate_error(exc)
        domain.state = SubscriptionCustomDomainState.failed
        logger.warning("Certificate issuance failed for domain id=%s", domain.id)
        return

    domain.certificate_ref = ref
    domain.certificate_not_after = not_after
    domain.certificate_issue_attempts = 0
    domain.certificate_issue_locked_until = None
    not_after_utc = not_after if not_after.tzinfo else not_after.replace(tzinfo=UTC)
    if not_after_utc <= dt.now(UTC):
        domain.state = SubscriptionCustomDomainState.failed
        domain.certificate_last_error = "certificate is not operational"
        return

    cert_ready = await provisioner.certificate_ready(domain)
    routable = await ingress_ready_for_hostname(
        domain,
        provisioner=provisioner,
        public_https_ready=custom_domain_settings.ingress_public_https_ready,
    )
    if cert_ready and routable:
        domain.certificate_last_error = None
        domain.state = SubscriptionCustomDomainState.active
        return

    domain.state = SubscriptionCustomDomainState.pending_certificate
    if not cert_ready:
        domain.certificate_last_error = "certificate is not yet ready at the ingress provider"
    elif not custom_domain_settings.ingress_public_https_ready:
        domain.certificate_last_error = (
            "public HTTPS ingress is not marked ready (CUSTOM_DOMAIN_INGRESS_PUBLIC_HTTPS_READY)"
        )
    else:
        domain.certificate_last_error = "ingress routing is not ready for this hostname"


async def process_custom_domain_certificates() -> None:
    provisioner = get_certificate_provisioner()
    async with GetDB() as db:
        pending = (
            await db.execute(
                select(SubscriptionCustomDomain)
                .where(
                    SubscriptionCustomDomain.state.in_(
                        [
                            SubscriptionCustomDomainState.pending_certificate,
                            SubscriptionCustomDomainState.renewal_pending,
                        ]
                    )
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()

        for domain in pending:
            await _process_issuance_row(domain, provisioner)

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
                continue
            if not certificate_is_operational(domain):
                domain.state = SubscriptionCustomDomainState.renewal_pending
                continue
            routable = await domain_is_routable_for_subscriptions(
                domain,
                provisioner=provisioner,
                public_https_ready=custom_domain_settings.ingress_public_https_ready,
            )
            if not routable:
                domain.state = SubscriptionCustomDomainState.pending_certificate
                domain.certificate_last_error = "ingress or certificate no longer operational"

        await db.commit()


if runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        process_custom_domain_certificates,
        "interval",
        seconds=custom_domain_settings.custom_domain_ssl_interval,
        max_instances=1,
        coalesce=True,
        id="process_custom_domain_certificates",
        replace_existing=True,
    )
