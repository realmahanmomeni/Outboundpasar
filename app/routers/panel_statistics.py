from fastapi import APIRouter, Depends

from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.panel_statistics import PanelStatisticsListResponse, PanelStatisticsOverviewResponse
from app.operation import OperatorType
from app.operation.panel_statistics import PanelStatisticsOperation
from app.utils import responses

from .authentication import require_permission

router = APIRouter(
    tags=["Panel statistics"],
    prefix="/api/statistics",
    responses={401: responses._401, 403: responses._403},
)

operator = PanelStatisticsOperation(OperatorType.API)


@router.get("/overview", response_model=PanelStatisticsOverviewResponse)
async def get_panel_statistics_overview(
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("users", "read")),
):
    return await operator.get_overview(db, admin)


@router.get("/panels", response_model=PanelStatisticsListResponse)
async def get_panel_statistics_panels(
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("users", "read")),
):
    return await operator.list_panels(db, admin)
