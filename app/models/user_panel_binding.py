from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models_oc import UserPanelBindingSource


class UserPanelBindingCreate(BaseModel):
    oc_panel_id: int = Field(..., gt=0)
    enabled: bool = True
    source: UserPanelBindingSource = UserPanelBindingSource.explicit
    desired_config_ids: list[str] | None = None


class UserPanelBindingUpdate(BaseModel):
    enabled: bool | None = None
    desired_config_ids: list[str] | None = None
    source: UserPanelBindingSource | None = None


class UserPanelBindingResponse(BaseModel):
    id: int
    tenant_id: int
    user_id: int
    oc_panel_id: int
    enabled: bool
    source: UserPanelBindingSource
    desired_config_ids: list[str] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UserPanelBindingListResponse(BaseModel):
    bindings: list[UserPanelBindingResponse]
    count: int
