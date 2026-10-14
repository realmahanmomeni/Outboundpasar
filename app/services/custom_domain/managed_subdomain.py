"""Outbound Center managed subscription subdomains under a fixed base domain."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime as dt
from enum import StrEnum

from app.db.models import SubscriptionCustomDomain, SubscriptionCustomDomainState
from app.services.custom_domain.normalization import DomainValidationError
from app.services.custom_domain.normalization import _LABEL_RE
from config import custom_domain_settings


class SubscriptionDomainKind(StrEnum):
    arbitrary = "arbitrary"
    oc_managed = "oc_managed"


class ManagedSubdomainValidationError(ValueError):
    pass


class ManagedSubdomainApiUnavailable(Exception):
    """OC ownership API is disabled or unreachable — fail closed for new associations."""


class ManagedSubdomainNotOwned(Exception):
    """Subdomain is not registered to the connected OC account."""


class ManagedSubdomainNotEligible(Exception):
    """Subdomain exists but is suspended, deleted, or not assignable."""


@dataclass(frozen=True, slots=True)
class ManagedSubdomainRecord:
    subdomain_id: str
    label: str
    fqdn: str
    status: str
    oc_account_id: int


def managed_base_domain() -> str:
    return custom_domain_settings.managed_subscription_base_domain.strip().lower().rstrip(".")


def fqdn_for_label(label: str) -> str:
    normalized_label = normalize_managed_subdomain_label(label)
    return f"{normalized_label}.{managed_base_domain()}"


def normalize_managed_subdomain_label(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        raise ManagedSubdomainValidationError("Subdomain label is required")
    candidate = raw.strip().lower().rstrip(".")
    if not candidate:
        raise ManagedSubdomainValidationError("Subdomain label is required")
    if "://" in candidate or "/" in candidate or "@" in candidate or ":" in candidate:
        raise ManagedSubdomainValidationError("Provide a single subdomain label only")
    base = managed_base_domain()
    if candidate == base or candidate.endswith(f".{base}"):
        if candidate == base:
            raise ManagedSubdomainValidationError("The apex domain cannot be used as a managed subdomain")
        suffix = candidate.removesuffix(f".{base}")
        if "." in suffix:
            raise ManagedSubdomainValidationError("Nested subdomains are not supported for managed hostnames")
        candidate = suffix
    if candidate.startswith("*.") or "*" in candidate:
        raise ManagedSubdomainValidationError("Wildcard subdomains are not supported")
    if not _LABEL_RE.match(candidate):
        raise ManagedSubdomainValidationError("Subdomain label contains invalid characters or length")
    return candidate


def normalize_managed_subdomain_input(raw: str) -> tuple[str, str]:
    """Return (label, fqdn) from a label or full managed hostname."""
    text = raw.strip().lower().rstrip(".")
    base = managed_base_domain()
    if text.endswith(f".{base}"):
        label = text.removesuffix(f".{base}")
        if "." in label:
            raise ManagedSubdomainValidationError("Nested subdomains are not supported")
        label = normalize_managed_subdomain_label(label)
        return label, f"{label}.{base}"
    label = normalize_managed_subdomain_label(text)
    return label, fqdn_for_label(label)


def is_managed_hostname(hostname: str) -> bool:
    try:
        base = managed_base_domain()
        host = hostname.lower().rstrip(".")
        if not host.endswith(f".{base}") or host == base:
            return False
        label = host.removesuffix(f".{base}")
        return "." not in label
    except Exception:
        return False


def reject_arbitrary_managed_hostname(hostname: str) -> None:
    if is_managed_hostname(hostname):
        raise DomainValidationError(
            f"Use the managed subdomain API for hostnames under {managed_base_domain()}"
        )


def managed_subdomain_is_active(domain: SubscriptionCustomDomain) -> bool:
    return (
        getattr(domain, "domain_kind", SubscriptionDomainKind.arbitrary.value)
        == SubscriptionDomainKind.oc_managed.value
        and domain.state == SubscriptionCustomDomainState.active
    )


def parse_managed_subdomain_payload(payload: dict) -> ManagedSubdomainRecord:
    if not isinstance(payload, dict):
        raise ManagedSubdomainApiUnavailable("invalid managed subdomain response")
    subdomain_id = str(payload.get("subdomain_id") or payload.get("id") or "").strip()
    label = str(payload.get("label") or "").strip().lower()
    fqdn = str(payload.get("fqdn") or payload.get("hostname") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower()
    account_raw = payload.get("oc_account_id")
    if not subdomain_id or not label or not status:
        raise ManagedSubdomainApiUnavailable("incomplete managed subdomain response")
    if not fqdn:
        fqdn = fqdn_for_label(label)
    try:
        oc_account_id = int(account_raw)
    except (TypeError, ValueError):
        raise ManagedSubdomainApiUnavailable("managed subdomain response missing oc_account_id")
    return ManagedSubdomainRecord(
        subdomain_id=subdomain_id,
        label=label,
        fqdn=fqdn.rstrip("."),
        status=status,
        oc_account_id=oc_account_id,
    )


_ELIGIBLE_OC_STATUSES = frozenset({"active", "ready"})


def assert_managed_record_eligible(record: ManagedSubdomainRecord, *, expected_account_id: int) -> None:
    if record.oc_account_id != expected_account_id:
        raise ManagedSubdomainNotOwned("Subdomain is not registered to the connected Outbound Center account")
    if record.status not in _ELIGIBLE_OC_STATUSES:
        raise ManagedSubdomainNotEligible(f"Subdomain is not eligible (status={record.status})")
    expected_fqdn = fqdn_for_label(record.label)
    if record.fqdn != expected_fqdn:
        raise ManagedSubdomainApiUnavailable("managed subdomain hostname mismatch from Outbound Center")


def managed_validation_fresh(domain: SubscriptionCustomDomain, *, max_age_seconds: int) -> bool:
    validated = getattr(domain, "oc_last_validated_at", None)
    if validated is None:
        return False
    if validated.tzinfo is None:
        validated = validated.replace(tzinfo=UTC)
    return (dt.now(UTC) - validated).total_seconds() <= max_age_seconds
