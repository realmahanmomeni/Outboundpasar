from __future__ import annotations

from pydantic import BaseModel, Field


class BotManagedSubdomainBindingCreate(BaseModel):
    """Caller identity is derived from X-OC-Connection-Token — do not send workspace/admin ids."""

    label: str = Field(min_length=1, max_length=63)
    oc_subdomain_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)


class BotManagedSubdomainBindingResponse(BaseModel):
    id: int
    label: str
    fqdn: str
    status: str
    workspace_id: int
    binding_admin_id: int


class ManagedSubdomainSettingsResponse(BaseModel):
    domain_suffix: str
    label: str | None = None
    fqdn: str | None = None
    pending_claim_label: str | None = None
    state: str | None = None
    ownership_linked: bool = False
    serving_ready: bool = False


class ManagedSubdomainSettingsUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=63)
