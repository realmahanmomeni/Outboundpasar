from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.general import get_system_usage
from app.db.crud.panel_statistics import (
    aggregate_panel_statistics,
    count_panels_in_scope,
    sum_mapping_traffic_in_scope,
)
from app.db.crud.user import get_users_count_metrics
from app.db.models import UserStatus
from app.models.admin import AdminDetails
from app.models.panel_statistics import (
    PanelExpirationDistribution,
    PanelStatisticsItem,
    PanelStatisticsListResponse,
    PanelStatisticsOverviewResponse,
)
from app.operation import BaseOperation
from app.operation.user import _actor_workspace_id
from app.services.oc_actor_connection import resolve_oc_telegram_connection_for_actor
from app.services.tenant_admin_scope import require_admin_tenant_id
from app.services.workspace_scope import require_admin_workspace_id
from config import panel_statistics_settings


class PanelStatisticsOperation(BaseOperation):
    async def _ensure_enabled(self) -> None:
        if not panel_statistics_settings.enabled:
            await self.raise_error("Panel statistics dashboard is disabled on this deployment", 503)

    @staticmethod
    async def _tenant_oc_panel_filter(
        db: AsyncSession,
        admin: AdminDetails,
        *,
        is_owner: bool,
    ) -> tuple[int, str, int | None]:
        tenant_id = await require_admin_tenant_id(db, admin, is_owner)
        connection = await resolve_oc_telegram_connection_for_actor(db, admin, is_owner, tenant_id)
        if connection is None or connection.oc_account_id is None:
            return tenant_id, "disconnected", None
        return tenant_id, "connected", connection.oc_account_id

    async def _scope(
        self,
        db: AsyncSession,
        admin: AdminDetails,
    ) -> tuple[int | None, int | None, int | None]:
        is_owner = admin.is_owner
        workspace_id = await _actor_workspace_id(db, admin)
        tenant_id = None
        oc_account_id = None
        if not is_owner:
            tenant_id, oc_state, oc_account_id = await self._tenant_oc_panel_filter(db, admin, is_owner=is_owner)
            if oc_state == "disconnected":
                oc_account_id = -1  # sentinel: no panels visible
            workspace_id = await require_admin_workspace_id(db, admin, is_owner)
        return workspace_id, tenant_id, oc_account_id

    async def get_overview(self, db: AsyncSession, admin: AdminDetails) -> PanelStatisticsOverviewResponse:
        await self._ensure_enabled()
        workspace_id, tenant_id, oc_account_id = await self._scope(db, admin)
        if oc_account_id == -1:
            return PanelStatisticsOverviewResponse()

        statuses = [UserStatus.active, UserStatus.disabled, UserStatus.on_hold, UserStatus.expired, UserStatus.limited]
        admin_id = None
        counts_workspace = workspace_id
        if admin.is_owner and workspace_id is None:
            counts_workspace = None

        user_counts, online_users = await get_users_count_metrics(
            db,
            statuses,
            timedelta(minutes=2),
            admin_id,
            workspace_id=counts_workspace,
        )

        if admin.is_owner and workspace_id is None:
            system = await get_system_usage(db)
            traffic_used = int((system.uplink or 0) + (system.downlink or 0)) if system else 0
        elif workspace_id is not None:
            traffic_used = await sum_mapping_traffic_in_scope(
                db,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                oc_account_id=oc_account_id if oc_account_id != -1 else None,
            )
        else:
            traffic_used = int(admin.used_traffic or 0)

        panel_count = await count_panels_in_scope(
            db,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            oc_account_id=oc_account_id if oc_account_id != -1 else None,
        )
        if oc_account_id == -1:
            panel_count = 0

        active_users = int(user_counts.get(UserStatus.active.value, 0))
        expired_users = int(user_counts.get(UserStatus.expired.value, 0))

        return PanelStatisticsOverviewResponse(
            total_users=int(user_counts.get("total", 0)),
            active_users=active_users,
            expired_users=expired_users,
            online_users=int(online_users),
            active_subscriptions=active_users,
            expired_subscriptions=expired_users,
            traffic_used=traffic_used,
            panel_count=panel_count,
        )

    async def list_panels(self, db: AsyncSession, admin: AdminDetails) -> PanelStatisticsListResponse:
        await self._ensure_enabled()
        workspace_id, tenant_id, oc_account_id = await self._scope(db, admin)
        if oc_account_id == -1:
            return PanelStatisticsListResponse(panels=[])

        rows = await aggregate_panel_statistics(
            db,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            oc_account_id=oc_account_id,
        )
        panels = [
            PanelStatisticsItem(
                panel_id=row["panel_id"],
                panel_name=row["panel_name"],
                sync_status=row["sync_status"],
                total_users=row["total_users"],
                active_users=row["active_users"],
                traffic_used=row["traffic_used"],
                last_activity_at=row["last_activity_at"],
                expiration_distribution=PanelExpirationDistribution(**row["expiration_distribution"]),
            )
            for row in rows
        ]
        return PanelStatisticsListResponse(panels=panels)
