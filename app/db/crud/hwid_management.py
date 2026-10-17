"""Workspace-scoped HWID management aggregations (Phase 3-G)."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, UserHWID


def _workspace_user_filter(workspace_id: int | None):
    if workspace_id is not None:
        return User.workspace_id == workspace_id
    return True


async def get_hwid_row_by_id(db: AsyncSession, device_id: int) -> UserHWID | None:
    stmt = select(UserHWID).where(UserHWID.id == device_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def aggregate_hwid_summary(db: AsyncSession, *, workspace_id: int | None) -> dict[str, int]:
    join = UserHWID.user_id == User.id
    device_stmt = (
        select(func.count(UserHWID.id))
        .select_from(UserHWID)
        .join(User, join)
        .where(_workspace_user_filter(workspace_id))
    )
    users_stmt = (
        select(func.count(func.distinct(UserHWID.user_id)))
        .select_from(UserHWID)
        .join(User, join)
        .where(_workspace_user_filter(workspace_id))
    )
    device_count = int((await db.execute(device_stmt)).scalar_one() or 0)
    users_with_devices = int((await db.execute(users_stmt)).scalar_one() or 0)
    return {
        "device_count": device_count,
        "users_with_devices": users_with_devices,
        "blocked_devices": 0,
    }


async def list_workspace_hwid_users(
    db: AsyncSession,
    *,
    workspace_id: int | None,
    offset: int,
    limit: int,
) -> tuple[list[dict], int]:
    join = UserHWID.user_id == User.id
    base = (
        select(
            User.id.label("user_id"),
            User.username,
            User.hwid_limit,
            func.count(UserHWID.id).label("active_devices"),
            func.max(UserHWID.last_used_at).label("last_activity_at"),
        )
        .select_from(User)
        .join(UserHWID, join, isouter=False)
        .where(_workspace_user_filter(workspace_id))
        .group_by(User.id, User.username, User.hwid_limit)
    )
    count_stmt = select(func.count()).select_from(base.subquery())
    total = int((await db.execute(count_stmt)).scalar_one() or 0)
    rows_stmt = base.order_by(func.max(UserHWID.last_used_at).desc()).offset(offset).limit(limit)
    result = await db.execute(rows_stmt)
    rows = [
        {
            "user_id": r.user_id,
            "username": r.username,
            "hwid_limit": r.hwid_limit,
            "active_devices": int(r.active_devices or 0),
            "last_activity_at": r.last_activity_at,
        }
        for r in result.all()
    ]
    return rows, total
