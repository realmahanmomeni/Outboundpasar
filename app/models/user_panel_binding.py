from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models_oc import UserPanelBindingSource

MAX_SELLER_GROUP_SELECTION = 64


def normalize_seller_group_ids(value: list[str] | None) -> list[str] | None:
    """
    Canonical form of an explicit seller group selection.

    ``None`` means "not provided". A provided list must be non-empty: an empty selection is
    rejected instead of being interpreted as "seller default" or "all groups".
    Ids are trimmed, de-duplicated and kept in first-seen order.
    """
    if value is None:
        return None
    seen: list[str] = []
    for raw in value:
        text = str(raw).strip()
        if not text:
            raise ValueError("Seller group ids must be non-empty strings")
        if len(text) > 256:
            raise ValueError("Seller group id is too long")
        if text not in seen:
            seen.append(text)
    if not seen:
        raise ValueError("Select at least one seller group (an empty selection is not allowed)")
    if len(seen) > MAX_SELLER_GROUP_SELECTION:
        raise ValueError(f"At most {MAX_SELLER_GROUP_SELECTION} seller groups can be selected")
    return seen


class UserPanelBindingCreate(BaseModel):
    oc_panel_id: int = Field(..., gt=0)
    enabled: bool = True
    source: UserPanelBindingSource = UserPanelBindingSource.explicit
    desired_config_ids: list[str] | None = None
    # Additive: seller group ids explicitly chosen for this user on this panel (OC source group ids).
    desired_source_group_ids: list[str] | None = None

    @field_validator("desired_source_group_ids")
    @classmethod
    def _validate_groups(cls, value: list[str] | None) -> list[str] | None:
        return normalize_seller_group_ids(value)


class UserPanelBindingUpdate(BaseModel):
    enabled: bool | None = None
    desired_config_ids: list[str] | None = None
    source: UserPanelBindingSource | None = None
    desired_source_group_ids: list[str] | None = None

    @field_validator("desired_source_group_ids")
    @classmethod
    def _validate_groups(cls, value: list[str] | None) -> list[str] | None:
        return normalize_seller_group_ids(value)


class UserPanelBindingResponse(BaseModel):
    id: int
    tenant_id: int
    user_id: int
    oc_panel_id: int
    enabled: bool
    source: UserPanelBindingSource
    desired_config_ids: list[str] | None = None
    desired_source_group_ids: list[str] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UserPanelBindingListResponse(BaseModel):
    bindings: list[UserPanelBindingResponse]
    count: int


class SellerGroupOption(BaseModel):
    """One imported seller group on a panel."""

    source_group_id: str
    name: str
    # Eligible = selected by the administrator during panel import; only eligible groups may be assigned.
    eligible: bool
    # Deprecated: reflects the stored per-user value, which no longer influences provisioning.
    selected_for_user: bool = False


class UserSellerGroupPanel(BaseModel):
    panel_id: int
    panel_name: str
    source_panel_id: str
    groups: list[SellerGroupOption]
    # Deprecated: stored per-user value, informational only.
    explicit_group_ids: list[str] | None = None
    binding_id: int | None = None
    # True when the panel's import selection is missing, so the seller account is NOT created/updated
    # until the admin selects groups in the panel import.
    selection_required: bool = False
    selection_issue: str | None = None
    mapping_status: str | None = None
    # Additive: source group ids selected during panel import = the groups every user of this panel receives.
    import_selected_group_ids: list[str] = []
    selection_message: str | None = None


class UserSellerGroupsResponse(BaseModel):
    user_id: int | None = None
    panels: list[UserSellerGroupPanel]
