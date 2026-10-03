from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.user_panel_binding import (
    create_user_panel_binding,
    delete_user_panel_binding,
    get_user_panel_binding,
    list_user_panel_bindings,
)
from app.db.models import User
from app.db.models_oc import OCPanel, UserPanelBinding, UserPanelBindingSource
from app.models.admin import AdminDetails
from app.models.user_panel_binding import (
    UserPanelBindingCreate,
    UserPanelBindingListResponse,
    UserPanelBindingResponse,
    UserPanelBindingUpdate,
)
from app.operation import BaseOperation


class UserPanelBindingOperation(BaseOperation):
    async def _user_tenant_id(self, db_user: User) -> int | None:
        if db_user.admin_id is None:
            return None
        if db_user.admin is not None:
            return db_user.admin.tenant_id
        from app.db.crud.admin import get_admin_by_id

        db_admin = await get_admin_by_id(db, db_user.admin_id)
        return db_admin.tenant_id if db_admin else None

    async def _get_panel_or_404(self, db: AsyncSession, oc_panel_id: int) -> OCPanel:
        panel = (await db.execute(select(OCPanel).where(OCPanel.id == oc_panel_id))).scalar_one_or_none()
        if not panel:
            await self.raise_error(message="Panel not found", code=404)
        return panel

    async def _ensure_panel_tenant_compatible(
        self,
        panel: OCPanel,
        user_tenant_id: int | None,
        admin: AdminDetails,
    ) -> None:
        if admin.is_owner:
            return
        if user_tenant_id is None or panel.tenant_id is None:
            await self.raise_error(message="Panel not found", code=404)
        if panel.tenant_id != user_tenant_id or panel.tenant_id != admin.tenant_id:
            await self.raise_error(message="Panel not found", code=404)

    async def _ensure_owner_cross_tenant_panel(
        self,
        panel: OCPanel,
        user_tenant_id: int | None,
    ) -> None:
        """Owner may cross tenants, but user and panel tenants must still align when both are set."""
        if user_tenant_id is None or panel.tenant_id is None:
            return
        if panel.tenant_id != user_tenant_id:
            await self.raise_error(message="Panel not found", code=404)

    async def _get_binding_for_user(
        self, db: AsyncSession, user_id: int, binding_id: int
    ) -> UserPanelBinding:
        binding = await get_user_panel_binding(db, user_id, binding_id)
        if not binding:
            await self.raise_error(message="Panel binding not found", code=404)
        return binding

    def _to_response(self, binding: UserPanelBinding) -> UserPanelBindingResponse:
        return UserPanelBindingResponse.model_validate(binding)

    async def list_bindings(
        self, db: AsyncSession, user_id: int, admin: AdminDetails
    ) -> UserPanelBindingListResponse:
        await self.get_validated_user_by_id(db, user_id, admin)
        bindings = await list_user_panel_bindings(db, user_id)
        responses = [self._to_response(b) for b in bindings]
        return UserPanelBindingListResponse(bindings=responses, count=len(responses))

    async def create_binding(
        self,
        db: AsyncSession,
        user_id: int,
        payload: UserPanelBindingCreate,
        admin: AdminDetails,
    ) -> UserPanelBindingResponse:
        db_user = await self.get_validated_user_by_id(
            db, user_id, admin, scope_action="update", load_admin=True
        )
        panel = await self._get_panel_or_404(db, payload.oc_panel_id)
        user_tenant_id = await self._user_tenant_id(db_user)
        if admin.is_owner:
            await self._ensure_owner_cross_tenant_panel(panel, user_tenant_id)
        else:
            await self._ensure_panel_tenant_compatible(panel, user_tenant_id, admin)

        if user_tenant_id is None:
            await self.raise_error(message="User tenant could not be resolved", code=400)

        binding = UserPanelBinding(
            tenant_id=user_tenant_id,
            user_id=db_user.id,
            oc_panel_id=panel.id,
            enabled=payload.enabled,
            source=payload.source.value,
            desired_config_ids=payload.desired_config_ids,
        )
        try:
            binding = await create_user_panel_binding(db, binding)
        except IntegrityError:
            await self.raise_error(message="Panel binding already exists for this user", code=409, db=db)
        return self._to_response(binding)

    async def update_binding(
        self,
        db: AsyncSession,
        user_id: int,
        binding_id: int,
        payload: UserPanelBindingUpdate,
        admin: AdminDetails,
    ) -> UserPanelBindingResponse:
        await self.get_validated_user_by_id(db, user_id, admin, scope_action="update")
        binding = await self._get_binding_for_user(db, user_id, binding_id)
        if not admin.is_owner and binding.tenant_id != admin.tenant_id:
            await self.raise_error(message="Panel binding not found", code=404)

        if payload.enabled is not None:
            binding.enabled = payload.enabled
        if payload.desired_config_ids is not None:
            binding.desired_config_ids = payload.desired_config_ids
        if payload.source is not None:
            binding.source = payload.source.value

        await db.flush()
        await db.refresh(binding)
        return self._to_response(binding)

    async def delete_binding(
        self, db: AsyncSession, user_id: int, binding_id: int, admin: AdminDetails
    ) -> None:
        await self.get_validated_user_by_id(db, user_id, admin, scope_action="update")
        binding = await self._get_binding_for_user(db, user_id, binding_id)
        if not admin.is_owner and binding.tenant_id != admin.tenant_id:
            await self.raise_error(message="Panel binding not found", code=404)
        await delete_user_panel_binding(db, binding)
