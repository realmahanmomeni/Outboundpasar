from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PanelExpirationDistribution(BaseModel):
    active: int = 0
    expired: int = 0
    disabled: int = 0
    limited: int = 0
    on_hold: int = 0


class PanelStatisticsItem(BaseModel):
    panel_id: int
    panel_name: str
    sync_status: str | None = None
    total_users: int = 0
    active_users: int = 0
    traffic_used: int = 0
    last_activity_at: datetime | None = None
    expiration_distribution: PanelExpirationDistribution = Field(default_factory=PanelExpirationDistribution)


class PanelStatisticsListResponse(BaseModel):
    panels: list[PanelStatisticsItem] = Field(default_factory=list)


class PanelStatisticsOverviewResponse(BaseModel):
    total_users: int = 0
    active_users: int = 0
    expired_users: int = 0
    online_users: int = 0
    active_subscriptions: int = 0
    expired_subscriptions: int = 0
    traffic_used: int = 0
    panel_count: int = 0
