from fastapi import APIRouter, Depends, status

from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.subscription_experience_template import (
    SubscriptionExperienceActivateRequest,
    SubscriptionExperiencePreviewRequest,
    SubscriptionExperiencePreviewResponse,
    SubscriptionExperienceTemplateCreate,
    SubscriptionExperienceTemplateListResponse,
    SubscriptionExperienceTemplateModify,
    SubscriptionExperienceTemplateResponse,
)
from app.operation import OperatorType
from app.operation.subscription_experience_template import SubscriptionExperienceTemplateOperation
from app.utils import responses

from .authentication import require_permission

router = APIRouter(
    tags=["Subscription experience templates"],
    prefix="/api/subscription/templates",
    responses={401: responses._401, 403: responses._403},
)

operator = SubscriptionExperienceTemplateOperation(OperatorType.API)


@router.get("", response_model=SubscriptionExperienceTemplateListResponse)
async def list_subscription_experience_templates(
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "read")),
):
    return await operator.list_templates(db, admin)


@router.post("", response_model=SubscriptionExperienceTemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_subscription_experience_template(
    payload: SubscriptionExperienceTemplateCreate,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    return await operator.create_template(db, payload, admin)


@router.put("/{template_id}", response_model=SubscriptionExperienceTemplateResponse)
async def update_subscription_experience_template(
    template_id: int,
    payload: SubscriptionExperienceTemplateModify,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    return await operator.update_template(db, template_id, payload, admin)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscription_experience_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    await operator.delete_template(db, template_id, admin)
    return {}


@router.post("/activate", response_model=SubscriptionExperienceTemplateListResponse)
async def activate_subscription_experience_template(
    payload: SubscriptionExperienceActivateRequest,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("settings", "update")),
):
    return await operator.activate_template(db, payload, admin)


@router.post("/preview", response_model=SubscriptionExperiencePreviewResponse)
async def preview_subscription_experience_template(
    payload: SubscriptionExperiencePreviewRequest,
    admin: AdminDetails = Depends(require_permission("settings", "read")),
):
    return await operator.preview_template(payload)
