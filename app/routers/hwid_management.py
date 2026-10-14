from fastapi import APIRouter, Depends, Query

from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.hwid_management import (
    HWIDManagementResetResponse,
    HWIDManagementRevokeResponse,
    HWIDManagementSummaryResponse,
    HWIDManagementUserDetailResponse,
    HWIDManagementUserListResponse,
)
from app.operation import OperatorType
from app.operation.hwid_management import HWIDManagementOperation
from app.utils import responses

from .authentication import require_permission

router = APIRouter(
    tags=["HWID management"],
    prefix="/api/hwid",
    responses={401: responses._401, 403: responses._403},
)

operator = HWIDManagementOperation(OperatorType.API)


@router.get("/summary", response_model=HWIDManagementSummaryResponse)
async def get_hwid_summary(
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hwids", "read")),
):
    return await operator.get_summary(db, admin)


@router.get("/users", response_model=HWIDManagementUserListResponse)
async def list_hwid_users(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hwids", "read")),
):
    return await operator.list_users(db, admin, offset=offset, limit=limit)


@router.get("/users/{user_id}", response_model=HWIDManagementUserDetailResponse)
async def get_hwid_user_detail(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hwids", "read")),
):
    return await operator.get_user_detail(db, user_id, admin)


@router.post("/devices/{device_id}/revoke", response_model=HWIDManagementRevokeResponse)
async def revoke_hwid_device(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hwids", "delete")),
):
    return await operator.revoke_device(db, device_id, admin)


@router.post("/users/{user_id}/reset", response_model=HWIDManagementResetResponse)
async def reset_hwid_user_devices(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("hwids", "delete")),
):
    return await operator.reset_user_devices(db, user_id, admin)
