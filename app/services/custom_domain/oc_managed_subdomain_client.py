"""Server-to-server Outbound Center managed subdomain ownership lookups."""
from __future__ import annotations

from typing import Protocol

from app.db.models_oc import OCIntegration
from app.services.custom_domain.managed_subdomain import (
    ManagedSubdomainApiUnavailable,
    ManagedSubdomainNotOwned,
    ManagedSubdomainRecord,
    assert_managed_record_eligible,
    fqdn_for_label,
    normalize_managed_subdomain_label,
    parse_managed_subdomain_payload,
)
from app.services.oc_integration_client import OcIntegrationApiError, call_oc_api
from config import custom_domain_settings


class ManagedSubdomainOwnershipClient(Protocol):
    async def fetch_owned_subdomain(
        self,
        integration: OCIntegration,
        *,
        connection_token: str,
        label: str,
        expected_account_id: int,
    ) -> ManagedSubdomainRecord: ...


class OutboundCenterManagedSubdomainClient:
    """Calls the documented OC integration endpoint (not implemented on OC until deployed)."""

    async def fetch_owned_subdomain(
        self,
        integration: OCIntegration,
        *,
        connection_token: str,
        label: str,
        expected_account_id: int,
    ) -> ManagedSubdomainRecord:
        if not custom_domain_settings.oc_managed_subdomain_api_enabled:
            raise ManagedSubdomainApiUnavailable(
                "Outbound Center managed subdomain API is not enabled on PasarGuard"
            )
        normalized = normalize_managed_subdomain_label(label)
        path_template = custom_domain_settings.oc_managed_subdomain_lookup_path
        path = path_template.format(label=normalized)
        try:
            payload = await call_oc_api(
                integration,
                "GET",
                path,
                connection_token=connection_token,
            )
        except OcIntegrationApiError as exc:
            if exc.oc_status == 404:
                raise ManagedSubdomainNotOwned("Subdomain is not registered on Outbound Center") from exc
            raise ManagedSubdomainApiUnavailable("Outbound Center subdomain lookup failed") from exc
        record = parse_managed_subdomain_payload(payload if isinstance(payload, dict) else {})
        assert_managed_record_eligible(record, expected_account_id=expected_account_id)
        if record.fqdn != fqdn_for_label(normalized):
            raise ManagedSubdomainApiUnavailable("Outbound Center returned an unexpected hostname")
        return record


_client: ManagedSubdomainOwnershipClient = OutboundCenterManagedSubdomainClient()


def get_managed_subdomain_client() -> ManagedSubdomainOwnershipClient:
    return _client


def set_managed_subdomain_client(client: ManagedSubdomainOwnershipClient) -> None:
    global _client
    _client = client


def required_oc_api_contract() -> dict:
    """Document the API Outbound Center must implement (PasarGuard does not modify OC in this phase)."""
    return {
        "method": "GET",
        "path": "/v1/integration/pasarguard/subdomains/{label}",
        "authentication": {
            "X-Integration-Token": "PasarGuard integration token (server-to-server)",
            "X-OC-Connection-Token": "Per-tenant Telegram connection token (scopes to OC account)",
        },
        "success_response": {
            "subdomain_id": "stable string id",
            "label": "alpha",
            "fqdn": "alpha.outboundcenter.ir",
            "status": "active | suspended | deleted",
            "oc_account_id": "integer, must match the connection token account",
        },
        "errors": {
            "404": "Subdomain not registered or not visible to this account",
            "403": "Connection token invalid or account banned",
            "409": "Subdomain suspended or not eligible",
        },
        "ownership_rules": [
            "PasarGuard must not infer ownership from usernames or client-supplied account ids.",
            "Reassignments on OC must be visible via status or 404 on subsequent lookups.",
            "Responses must be signed by the integration trust boundary (tokens), not forgeable by browsers.",
        ],
    }
