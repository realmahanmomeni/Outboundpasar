from __future__ import annotations

from datetime import UTC

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.hwid import delete_user_hwid, get_user_hwids, reset_user_hwids
from app.db.crud.hwid_management import (
    aggregate_hwid_summary,
    get_hwid_row_by_id,
    list_workspace_hwid_users,
)
from app.models.admin import AdminDetails
from app.models.hwid_management import (
    HWIDManagementResetResponse,
    HWIDManagementRevokeResponse,
    HWIDManagementSummaryResponse,
    HWIDManagementUserBrief,
    HWIDManagementUserDetailResponse,
    HWIDManagementUserInfo,
    HWIDManagementUserListResponse,
)
from app.models.user import UserHWIDResponse
from app.operation import BaseOperation
from app.operation.user import _actor_workspace_id
from config import hwid_management_settings


def _device_status(hwid_limit: int | None, active_devices: int) -> str:
    if hwid_limit == 0:
        return "disabled"
    if hwid_limit is not None and hwid_limit > 0 and active_devices >= hwid_limit:
        return "at_limit"
    return "active"


def _user_hwid_status(hwid_limit: int | None, device_count: int) -> str:
    return _device_status(hwid_limit, device_count)


class HWIDManagementOperation(BaseOperation):
    async def _ensure_enabled(self) -> None:
        if not hwid_management_settings.enabled:
            await self.raise_error("HWID management is disabled on this deployment", 503)

    async def _workspace_scope(self, db: AsyncSession, admin: AdminDetails) -> int | None:
        return await _actor_workspace_id(db, admin)

    async def get_summary(self, db: AsyncSession, admin: AdminDetails) -> HWIDManagementSummaryResponse:
        await self._ensure_enabled()
        workspace_id = await self._workspace_scope(db, admin)
        data = await aggregate_hwid_summary(db, workspace_id=workspace_id)
        return HWIDManagementSummaryResponse(**data)

    async def list_users(
        self,
        db: AsyncSession,
        admin: AdminDetails,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> HWIDManagementUserListResponse:
        await self._ensure_enabled()
        workspace_id = await self._workspace_scope(db, admin)
        rows, total = await list_workspace_hwid_users(
            db, workspace_id=workspace_id, offset=offset, limit=limit
        )
        users = [
            HWIDManagementUserBrief(
                user_id=row["user_id"],
                username=row["username"],
                active_devices=row["active_devices"],
                last_activity_at=row["last_activity_at"],
                device_status=_device_status(row["hwid_limit"], row["active_devices"]),
            )
            for row in rows
        ]
        return HWIDManagementUserListResponse(users=users, total=total)

    async def get_user_detail(
        self, db: AsyncSession, user_id: int, admin: AdminDetails
    ) -> HWIDManagementUserDetailResponse:
        await self._ensure_enabled()
        db_user = await self.get_validated_user_by_id(db, user_id, admin)
        hwids = await get_user_hwids(db, db_user.id)
        last_activity = None
        for h in hwids:
            ts = h.last_used_at
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if last_activity is None or (ts is not None and ts > last_activity):
                last_activity = ts
        devices = [UserHWIDResponse.model_validate(h) for h in hwids]
        info = HWIDManagementUserInfo(
            user_id=db_user.id,
            username=db_user.username,
            hwid_limit=db_user.hwid_limit,
            device_count=len(devices),
            last_activity_at=last_activity,
            status=_user_hwid_status(db_user.hwid_limit, len(devices)),
        )
        return HWIDManagementUserDetailResponse(user=info, devices=devices)

    async def revoke_device(
        self, db: AsyncSession, device_id: int, admin: AdminDetails
    ) -> HWIDManagementRevokeResponse:
        await self._ensure_enabled()
        row = await get_hwid_row_by_id(db, device_id)
        if row is None:
            await self.raise_error(message="Device not found", code=404)
        db_user = await self.get_validated_user_by_id(db, row.user_id, admin, scope_action="delete")
        if row.user_id != db_user.id:
            await self.raise_error(message="Device not found", code=404)
        deleted = await delete_user_hwid(db, db_user.id, row.hwid)
        if not deleted:
            await self.raise_error(message="Device not found", code=404)
        return HWIDManagementRevokeResponse()

    async def reset_user_devices(
        self, db: AsyncSession, user_id: int, admin: AdminDetails
    ) -> HWIDManagementResetResponse:
        await self._ensure_enabled()
        db_user = await self.get_validated_user_by_id(db, user_id, admin, scope_action="delete")
        count = await reset_user_hwids(db, db_user.id)
        return HWIDManagementResetResponse(count=count)
