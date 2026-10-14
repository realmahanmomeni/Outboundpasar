from __future__ import annotations

from datetime import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from app.models.user import UserHWIDResponse


class HWIDManagementSummaryResponse(BaseModel):
    device_count: int = 0
    users_with_devices: int = 0
    blocked_devices: int = Field(
        default=0,
        description="Reserved for future soft-revoke state; always 0 with current storage model.",
    )


class HWIDManagementUserBrief(BaseModel):
    user_id: int
    username: str
    active_devices: int
    last_activity_at: dt | None = None
    device_status: str


class HWIDManagementUserListResponse(BaseModel):
    users: list[HWIDManagementUserBrief]
    total: int


class HWIDManagementUserInfo(BaseModel):
    user_id: int
    username: str
    hwid_limit: int | None = None
    device_count: int
    last_activity_at: dt | None = None
    status: str


class HWIDManagementUserDetailResponse(BaseModel):
    user: HWIDManagementUserInfo
    devices: list[UserHWIDResponse]
    model_config = ConfigDict(from_attributes=True)


class HWIDManagementRevokeResponse(BaseModel):
    revoked: bool = True


class HWIDManagementResetResponse(BaseModel):
    count: int
