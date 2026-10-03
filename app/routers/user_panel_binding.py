from fastapi import APIRouter, Depends, status

from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.user_panel_binding import (
    UserPanelBindingCreate,
    UserPanelBindingListResponse,
    UserPanelBindingResponse,
    UserPanelBindingUpdate,
)
from app.operation import OperatorType
from app.operation.user_panel_binding import UserPanelBindingOperation
from app.utils import responses

from .authentication import require_permission

binding_operator = UserPanelBindingOperation(operator_type=OperatorType.API)
router = APIRouter(tags=["User Panel Bindings"], prefix="/api/users", responses={401: responses._401})


@router.get(
    "/{user_id}/panel-bindings",
    response_model=UserPanelBindingListResponse,
    responses={403: responses._403, 404: responses._404},
)
async def list_user_panel_bindings(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("users", "read")),
):
    return await binding_operator.list_bindings(db, user_id=user_id, admin=admin)


@router.post(
    "/{user_id}/panel-bindings",
    response_model=UserPanelBindingResponse,
    status_code=status.HTTP_201_CREATED,
    responses={403: responses._403, 404: responses._404, 409: responses._409},
)
async def create_user_panel_binding(
    user_id: int,
    body: UserPanelBindingCreate,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("users", "update")),
):
    return await binding_operator.create_binding(db, user_id=user_id, payload=body, admin=admin)


@router.patch(
    "/{user_id}/panel-bindings/{binding_id}",
    response_model=UserPanelBindingResponse,
    responses={403: responses._403, 404: responses._404},
)
async def update_user_panel_binding(
    user_id: int,
    binding_id: int,
    body: UserPanelBindingUpdate,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("users", "update")),
):
    return await binding_operator.update_binding(
        db, user_id=user_id, binding_id=binding_id, payload=body, admin=admin
    )


@router.delete(
    "/{user_id}/panel-bindings/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={403: responses._403, 404: responses._404},
)
async def delete_user_panel_binding(
    user_id: int,
    binding_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("users", "update")),
):
    await binding_operator.delete_binding(db, user_id=user_id, binding_id=binding_id, admin=admin)
