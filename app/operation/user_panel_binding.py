from fastapi import HTTPException
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
    UserSellerGroupsResponse,
)
from app.operation import BaseOperation
from app.services.oc_seller_groups import (
    build_seller_group_panels,
    build_user_seller_group_panels,
    inbound_tags_for_local_groups,
    load_panel_group_inventory,
    validate_explicit_selection,
)
from app.services.workspace_scope import ensure_actor_panel_access


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
        db: AsyncSession,
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
        try:
            await ensure_actor_panel_access(
                db, panel, is_owner=False, admin=admin, identity=None
            )
        except HTTPException:
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

    async def _validated_seller_groups(
        self, db: AsyncSession, panel: OCPanel, group_ids: list[str]
    ) -> list[str]:
        """Reject ids that are not imported+selected groups of this panel (never silently dropped)."""
        inventory = await load_panel_group_inventory(db, panel.id)
        valid, invalid = validate_explicit_selection(group_ids, inventory)
        if invalid:
            available = ", ".join(f"{inventory.names.get(g, g)} [{g}]" for g in inventory.eligible_ids) or "none"
            await self.raise_error(
                message=(
                    "Seller group(s) not available for this panel: "
                    + ", ".join(invalid)
                    + f". Available groups: {available}"
                ),
                code=400,
            )
        return valid

    def _panel_visibility_check(self, db: AsyncSession, admin: AdminDetails, *, user_tenant_id: int | None = None):
        async def _visible(panel: OCPanel) -> bool:
            if admin.is_owner:
                return True
            if panel.tenant_id is None or panel.tenant_id != admin.tenant_id:
                return False
            if user_tenant_id is not None and user_tenant_id != panel.tenant_id:
                return False
            try:
                await ensure_actor_panel_access(db, panel, is_owner=False, admin=admin, identity=None)
            except HTTPException:
                return False
            return True

        return _visible

    async def list_seller_groups(
        self, db: AsyncSession, user_id: int, admin: AdminDetails
    ) -> UserSellerGroupsResponse:
        db_user = await self.get_validated_user_by_id(db, user_id, admin)
        user_tenant_id = db_user.admin.tenant_id if db_user.admin is not None else None
        panels = await build_user_seller_group_panels(
            db, db_user, panel_visible=self._panel_visibility_check(db, admin, user_tenant_id=user_tenant_id)
        )
        return UserSellerGroupsResponse(user_id=db_user.id, panels=panels)

    async def seller_group_options_for_local_groups(
        self, db: AsyncSession, group_ids: list[int], admin: AdminDetails
    ) -> UserSellerGroupsResponse:
        """Create form: seller groups of the OC panels reachable through the chosen local groups."""
        tags = await inbound_tags_for_local_groups(db, group_ids[:200])
        panels = await build_seller_group_panels(
            db, tags, user_id=None, panel_visible=self._panel_visibility_check(db, admin)
        )
        return UserSellerGroupsResponse(user_id=None, panels=panels)

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
            await self._ensure_panel_tenant_compatible(db, panel, user_tenant_id, admin)

        if user_tenant_id is None:
            await self.raise_error(message="User tenant could not be resolved", code=400)

        seller_groups: list[str] | None = None
        if payload.desired_source_group_ids is not None:
            seller_groups = await self._validated_seller_groups(db, panel, payload.desired_source_group_ids)

        binding = UserPanelBinding(
            tenant_id=user_tenant_id,
            user_id=db_user.id,
            oc_panel_id=panel.id,
            enabled=payload.enabled,
            source=payload.source.value,
            desired_config_ids=payload.desired_config_ids,
            desired_source_group_ids=seller_groups,
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
        if not admin.is_owner:
            if binding.tenant_id != admin.tenant_id:
                await self.raise_error(message="Panel binding not found", code=404)
            panel = await self._get_panel_or_404(db, binding.oc_panel_id)
            await self._ensure_panel_tenant_compatible(db, panel, binding.tenant_id, admin)

        if payload.desired_source_group_ids is not None:
            # Deprecated per-user field: validated and stored (contract unchanged) but ignored by provisioning,
            # which always uses the panel's import selection.
            binding.desired_source_group_ids = await self._validated_seller_groups(
                db, await self._get_panel_or_404(db, binding.oc_panel_id), payload.desired_source_group_ids
            )
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
        if not admin.is_owner:
            if binding.tenant_id != admin.tenant_id:
                await self.raise_error(message="Panel binding not found", code=404)
            panel = await self._get_panel_or_404(db, binding.oc_panel_id)
            await self._ensure_panel_tenant_compatible(db, panel, binding.tenant_id, admin)
        await delete_user_panel_binding(db, binding)
