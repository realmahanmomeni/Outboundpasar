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
    @property
    def provider_kind(self):
        from app.services.custom_domain.ingress_provider import IngressProviderKind

        return IngressProviderKind.none

    async def provider_status(self, *, public_https_ready: bool):
        from app.services.custom_domain.ingress_provider import IngressProviderStatus

        return IngressProviderStatus(
            provider=self.provider_kind,
            configured=False,
            reachable=False,
            routing_capable=False,
            public_https_ready=public_https_ready,
            message="provider status not implemented",
        )

    async def hostname_routing_ready(self, domain: SubscriptionCustomDomain) -> bool:
        return False

    async def certificate_ready(self, domain: SubscriptionCustomDomain) -> bool:
        if domain.certificate_not_after is None:
            return False
        not_after = domain.certificate_not_after
        if not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=UTC)
        return not_after > dt.now(UTC)

    async def issue(self, domain: SubscriptionCustomDomain) -> tuple[str, dt]:
        raise NotImplementedError

    async def deprovision(self, domain: SubscriptionCustomDomain) -> None:
        return None


class UnconfiguredCertificateProvisioner(CertificateProvisioner):
    """Production default when no ingress path is configured — never issues or activates domains."""

    @property
    def provider_kind(self):
        from app.services.custom_domain.ingress_provider import IngressProviderKind

        return IngressProviderKind.none

    async def provider_status(self, *, public_https_ready: bool):
        from app.services.custom_domain.ingress_provider import IngressProviderStatus

        return IngressProviderStatus(
            provider=self.provider_kind,
            configured=False,
            reachable=False,
            routing_capable=False,
            public_https_ready=public_https_ready,
            message=(
                "Custom-domain ingress is not configured. Set CUSTOM_DOMAIN_INGRESS_PROVIDER "
                "(for example caddy) after a dedicated IP or external TLS terminator is available."
            ),
        )

    async def hostname_routing_ready(self, domain: SubscriptionCustomDomain) -> bool:
        return False

    async def certificate_ready(self, domain: SubscriptionCustomDomain) -> bool:
        return False

    async def issue(self, domain: SubscriptionCustomDomain) -> tuple[str, dt]:
        raise CertificateProviderUnavailable(
            "custom domain ingress provider is not configured on this deployment"
        )

    async def deprovision(self, domain: SubscriptionCustomDomain) -> None:
        return None


class StubCertificateProvisioner(CertificateProvisioner):
    """Test-only stub that never performs real ACME or stores private keys."""

    @property
    def provider_kind(self):
        from app.services.custom_domain.ingress_provider import IngressProviderKind

        return IngressProviderKind.stub

    async def provider_status(self, *, public_https_ready: bool):
        from app.services.custom_domain.ingress_provider import IngressProviderStatus

        return IngressProviderStatus(
            provider=self.provider_kind,
            configured=True,
            reachable=True,
            routing_capable=True,
            public_https_ready=public_https_ready,
            message="stub ingress provider (testing)",
        )

    async def hostname_routing_ready(self, domain: SubscriptionCustomDomain) -> bool:
        return True

    async def issue(self, domain: SubscriptionCustomDomain) -> tuple[str, dt]:
        ref = f"stub-{uuid.uuid4().hex}"
        not_after = dt.now(UTC) + td(days=90)
        return ref, not_after


_provisioner: CertificateProvisioner = UnconfiguredCertificateProvisioner()


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
    if domain.certificate_not_after is None:
        return False
    not_after = domain.certificate_not_after
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=UTC)
    return not_after > dt.now(UTC)


async def domain_certificate_is_operational(
    domain: SubscriptionCustomDomain,
    provisioner: CertificateProvisioner | None = None,
) -> bool:
    if domain.state != SubscriptionCustomDomainState.active:
        return False
    if not certificate_is_operational(domain):
        return False
    active = provisioner or get_certificate_provisioner()
    return await active.certificate_ready(domain)


async def ingress_ready_for_hostname(
    domain: SubscriptionCustomDomain,
    *,
    provisioner: CertificateProvisioner | None = None,
    public_https_ready: bool,
) -> bool:
    if not certificate_is_operational(domain):
        return False
    if not await (provisioner or get_certificate_provisioner()).certificate_ready(domain):
        return False
    if not public_https_ready:
        return False
    active = provisioner or get_certificate_provisioner()
    return await active.hostname_routing_ready(domain)


async def domain_is_routable_for_subscriptions(
    domain: SubscriptionCustomDomain,
    *,
    provisioner: CertificateProvisioner | None = None,
    public_https_ready: bool,
) -> bool:
    if domain.state != SubscriptionCustomDomainState.active:
        return False
    return await ingress_ready_for_hostname(
        domain,
        provisioner=provisioner,
        public_https_ready=public_https_ready,
    )


async def get_ingress_provider_status() -> "IngressProviderStatus":
    from app.services.custom_domain.ingress_provider import IngressProviderStatus
    from config import custom_domain_settings

    provisioner = get_certificate_provisioner()
    return await provisioner.provider_status(
        public_https_ready=custom_domain_settings.ingress_public_https_ready
    )


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
