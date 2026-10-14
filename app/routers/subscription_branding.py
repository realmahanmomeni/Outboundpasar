from fastapi import APIRouter, Depends, status

from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.subscription_branding import (
    SubscriptionAppearancePreviewRequest,
    SubscriptionAppearancePreviewResponse,
    SubscriptionBrandingResponse,
    SubscriptionBrandingUpsert,
)
from app.operation import OperatorType
from app.operation.subscription_branding import SubscriptionBrandingOperation
from app.utils import responses

from .authentication import require_permission

router = APIRouter(
    tags=["Subscription branding"],
    prefix="/api/subscription/branding",
    responses={401: responses._401, 403: responses._403},
)

operator = SubscriptionBrandingOperation(OperatorType.API)


@router.get("", response_model=SubscriptionBrandingResponse)
async def get_subscription_branding(
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "read")),
):
    return await operator.get_branding(db, admin)


@router.post("", response_model=SubscriptionBrandingResponse, status_code=status.HTTP_201_CREATED)
async def create_subscription_branding(
    payload: SubscriptionBrandingUpsert,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    return await operator.create_branding(db, payload, admin)


@router.put("", response_model=SubscriptionBrandingResponse)
async def update_subscription_branding(
    payload: SubscriptionBrandingUpsert,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    return await operator.update_branding(db, payload, admin)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscription_branding(
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    await operator.delete_branding(db, admin)
    return {}


@router.post("/preview", response_model=SubscriptionAppearancePreviewResponse)
async def preview_subscription_customer_page(
    payload: SubscriptionAppearancePreviewRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "read")),
):
    return await operator.preview_customer_page(db, admin, payload)
