from __future__ import annotations

from pydantic import BaseModel, Field


class BotManagedSubdomainBindingCreate(BaseModel):
    tenant_id: int = Field(gt=0)
    workspace_id: int = Field(gt=0)
    binding_admin_id: int = Field(gt=0)
    label: str = Field(min_length=1, max_length=63)
    oc_subdomain_id: str = Field(min_length=1, max_length=128)
    oc_account_id: int = Field(gt=0)
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


class ManagedSubdomainSettingsUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=63)
