"""Authoritative hostname normalization and validation for custom subscription domains."""
from __future__ import annotations

import ipaddress
import re
_MAX_HOSTNAME_LEN = 253
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_BLOCKED_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".invalid",
    ".test",
    ".example",
)


class DomainValidationError(ValueError):
    pass


def normalize_custom_domain_hostname(raw: str) -> str:
    """Return punycode-lowercase hostname without port, path, or scheme."""
    if not raw or not isinstance(raw, str):
        raise DomainValidationError("Domain is required")

    candidate = raw.strip()
    if not candidate:
        raise DomainValidationError("Domain is required")

    if "://" in candidate or "/" in candidate or "?" in candidate or "#" in candidate or "@" in candidate:
        raise DomainValidationError("Provide a hostname only (no URL path, query, credentials, or scheme)")

    if candidate.startswith("[") and "]" in candidate:
        raise DomainValidationError("IP literals are not accepted as custom domains")

    if ":" in candidate and not candidate.startswith("xn--"):
        host_part, maybe_port = candidate.rsplit(":", 1)
        if maybe_port.isdigit():
            candidate = host_part
        else:
            raise DomainValidationError("Ports are not accepted in custom domain hostnames")

    candidate = candidate.rstrip(".").lower()
    if not candidate or len(candidate) > _MAX_HOSTNAME_LEN:
        raise DomainValidationError("Invalid hostname length")

    if candidate.startswith("*.") or candidate == "*":
        raise DomainValidationError("Wildcard custom domains are not supported")

    try:
        ipaddress.ip_address(candidate)
        raise DomainValidationError("IP literals are not accepted as custom domains")
    except ValueError:
        pass

    if candidate in {"localhost", "localhost.localdomain"}:
        raise DomainValidationError("Localhost hostnames are not accepted")

    for suffix in _BLOCKED_SUFFIXES:
        if candidate == suffix.lstrip(".") or candidate.endswith(suffix):
            raise DomainValidationError("Internal or reserved hostnames are not accepted")

    try:
        ascii_host = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DomainValidationError("Invalid internationalized domain name") from exc

    labels = ascii_host.split(".")
    if len(labels) < 2:
        raise DomainValidationError("A public custom domain must include a registrable suffix")

    for label in labels:
        if not _LABEL_RE.match(label):
            raise DomainValidationError("Hostname contains invalid characters or label length")

    return ascii_host


def verification_txt_record_name(hostname_normalized: str) -> str:
    return f"_pasarguard-verification.{hostname_normalized}"


def verification_txt_record_value(token: str) -> str:
    return f"pasarguard-site-verification={token}"
