"""Provider-neutral certificate lifecycle for custom subscription domains."""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime as dt, timedelta as td

from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState


class CertificateProvisionerError(Exception):
    """Base error for certificate issuance; message is safe to store on the domain row."""


class CertificateRateLimited(CertificateProvisionerError):
    """Transient provider rate limit — scheduler may retry with backoff."""


class CertificateProviderUnavailable(CertificateProvisionerError):
    """Transient infrastructure outage — scheduler may retry with backoff."""


class CertificateProvisioner:
    async def issue(self, domain: SubscriptionCustomDomain) -> tuple[str, dt]:
        raise NotImplementedError

    async def deprovision(self, domain: SubscriptionCustomDomain) -> None:
        return None


class StubCertificateProvisioner(CertificateProvisioner):
    """Testable stub that never performs real ACME or stores private keys."""

    async def issue(self, domain: SubscriptionCustomDomain) -> tuple[str, dt]:
        ref = f"stub-{uuid.uuid4().hex}"
        not_after = dt.now(UTC) + td(days=90)
        return ref, not_after


_provisioner: CertificateProvisioner = StubCertificateProvisioner()


def get_certificate_provisioner() -> CertificateProvisioner:
    return _provisioner


def set_certificate_provisioner(provisioner: CertificateProvisioner) -> None:
    global _provisioner
    _provisioner = provisioner


def can_request_certificate(state: SubscriptionCustomDomainState) -> bool:
    return state in {
        SubscriptionCustomDomainState.verified,
        SubscriptionCustomDomainState.failed,
        SubscriptionCustomDomainState.renewal_pending,
    }


def certificate_is_operational(domain: SubscriptionCustomDomain) -> bool:
    if domain.state != SubscriptionCustomDomainState.active:
        return False
    if domain.certificate_not_after is None:
        return False
    not_after = domain.certificate_not_after
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=UTC)
    return not_after > dt.now(UTC)


def domain_eligible_for_certificate_job(domain: SubscriptionCustomDomain) -> bool:
    if domain.state not in {
        SubscriptionCustomDomainState.pending_certificate,
        SubscriptionCustomDomainState.renewal_pending,
    }:
        return False
    if domain.verified_at is None:
        return False
    return True


def new_certificate_job_nonce() -> str:
    return secrets.token_hex(16)


def sanitize_certificate_error(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()
    for secret_hint in ("key", "token", "password", "secret", "credential", "private"):
        if secret_hint in lowered:
            return "certificate provider error"
    return message[:512]
