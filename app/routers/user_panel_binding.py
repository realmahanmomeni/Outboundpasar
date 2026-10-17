from fastapi import APIRouter, Depends, Query, status

from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.user_panel_binding import (
    UserPanelBindingCreate,
    UserPanelBindingListResponse,
    UserPanelBindingResponse,
    UserPanelBindingUpdate,
    UserSellerGroupsResponse,
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


@router.get(
    "/{user_id}/oc-seller-groups",
    response_model=UserSellerGroupsResponse,
    responses={403: responses._403, 404: responses._404},
)
async def list_user_oc_seller_groups(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("users", "read")),
):
    """Imported seller groups per OC panel reachable by this user, with the explicit selection (read-only)."""
    return await binding_operator.list_seller_groups(db, user_id=user_id, admin=admin)


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


options_router = APIRouter(tags=["User Panel Bindings"], prefix="/api/oc-seller-groups", responses={401: responses._401})


@options_router.get(
    "",
    response_model=UserSellerGroupsResponse,
    responses={403: responses._403},
)
async def list_oc_seller_group_options(
    group_ids: list[int] = Query(default_factory=list, description="Local group ids selected in the user form"),
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("users", "read")),
):
    """
    Read-only: imported seller groups of the OC panels reachable through the given local groups.

    Used by the user create form, before the user (and therefore a panel binding) exists.
    """
    return await binding_operator.seller_group_options_for_local_groups(db, group_ids=group_ids, admin=admin)
