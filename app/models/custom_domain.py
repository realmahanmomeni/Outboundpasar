from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models import SubscriptionCustomDomainState


class CustomDomainCreate(BaseModel):
    hostname: str = Field(min_length=1, max_length=512)


class ManagedSubdomainAssociate(BaseModel):
    """Label or full hostname under the managed base domain (for example alpha or alpha.outboundcenter.ir)."""

    subdomain: str = Field(min_length=1, max_length=253)


class CustomDomainVerificationInstructions(BaseModel):
    record_type: str = "TXT"
    record_name: str
    record_value: str
    expires_at: datetime | None = None


class CustomDomainResponse(BaseModel):
    id: int
    hostname: str
    domain_kind: str = "arbitrary"
    state: SubscriptionCustomDomainState
    verified_at: datetime | None = None
    certificate_ref: str | None = None
    certificate_not_after: datetime | None = None
    certificate_last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, row) -> CustomDomainResponse:
        return cls(
            id=row.id,
            hostname=row.hostname_normalized,
            domain_kind=getattr(row, "domain_kind", "arbitrary") or "arbitrary",
            state=row.state,
            verified_at=row.verified_at,
            certificate_ref=row.certificate_ref,
            certificate_not_after=row.certificate_not_after,
            certificate_last_error=row.certificate_last_error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class CustomDomainCreateResponse(CustomDomainResponse):
    verification: CustomDomainVerificationInstructions | None = None


class CustomDomainIngressStatusResponse(BaseModel):
    provider: str
    configured: bool
    reachable: bool
    routing_capable: bool
    public_https_ready: bool
    can_activate_custom_domains: bool
    message: str
    architecture_options: list[dict[str, str]]
