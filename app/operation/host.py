import asyncio

from app import notification
from app.core.hosts import host_manager
from app.db import AsyncSession
from app.db.crud.host import (
    create_host,
    get_host_by_id,
    get_hosts,
    modify_host,
    remove_host,
    remove_hosts,
)
from app.db.models import ProxyHost
from app.models.admin import AdminDetails
from app.models.client_template import ClientTemplateType
from app.models.host import (
    BaseHost,
    BulkHostsActionResponse,
    BulkHostSelection,
    CreateHost,
    HostListQuery,
    RemoveHostsResponse,
)
from app.operation import BaseOperation
from app.services.workspace_scope import resolve_tenant_workspace_scope
from app.services.oc_panel_destination_hosts import update_oc_destination_host_display_name
from app.services.oc_share_link import is_oc_destination_inbound_tag
from app.utils.logger import get_logger

logger = get_logger("host-operation")


class HostOperation(BaseOperation):
    async def _resource_scope(self, db: AsyncSession, admin: AdminDetails) -> tuple[int | None, int | None]:
        if admin.is_owner:
            return None, None
        return await resolve_tenant_workspace_scope(db, admin)

    async def get_hosts(self, db: AsyncSession, query: HostListQuery, admin: AdminDetails) -> list[BaseHost]:
        tenant_id, workspace_id = await self._resource_scope(db, admin)
        return await get_hosts(db=db, query=query, tenant_id=tenant_id, workspace_id=workspace_id)

    async def validate_subscription_templates(
        self, db: AsyncSession, host: CreateHost, admin: AdminDetails
    ) -> None:
        if not host.subscription_templates or host.subscription_templates.xray is None:
            return

        db_template = await self.get_validated_client_template(
            db, host.subscription_templates.xray, admin=admin
        )
        if db_template.template_type != ClientTemplateType.xray_subscription.value:
            await self.raise_error("Selected template must be an Xray subscription template", 400, db=db)

    async def validate_ds_host(
        self,
        db: AsyncSession,
        host: CreateHost,
        host_id: int | None = None,
        admin: AdminDetails | None = None,
    ) -> ProxyHost:
        if (
            host.transport_settings
            and host.transport_settings.xhttp_settings
            and (nested_host := host.transport_settings.xhttp_settings.download_settings)
        ):
            if host_id and nested_host == host_id:
                return await self.raise_error("download host cannot be the same as the host", 400, db=db)
            tenant_id, workspace_id = (None, None)
            if admin is not None and not admin.is_owner:
                tenant_id, workspace_id = await self._resource_scope(db, admin)
            ds_host = await get_host_by_id(db, nested_host, tenant_id=tenant_id, workspace_id=workspace_id)
            if not ds_host:
                return await self.raise_error("download host not found", 404, db=db)
            if (
                ds_host.transport_settings
                and ds_host.transport_settings.get("xhttp_settings")
                and ds_host.transport_settings.get("xhttp_settings").get("download_settings")
            ):
                return await self.raise_error("download host cannot have a download host", 400, db=db)

    async def create_host(self, db: AsyncSession, new_host: CreateHost, admin: AdminDetails) -> BaseHost:
        if is_oc_destination_inbound_tag(new_host.inbound_tag):
            await self.raise_error(
                "OC destination hosts are created from Outbound Center subscription sync, not manually",
                400,
                db=db,
            )

        await self.validate_subscription_templates(db, new_host, admin)
        await self.validate_ds_host(db, new_host, admin=admin)

        await self.check_host_inbound_tags([new_host.inbound_tag])

        tenant_id, workspace_id = await self._resource_scope(db, admin)
        db_host = await create_host(
            db,
            new_host,
            tenant_id=tenant_id if not admin.is_owner else None,
            workspace_id=workspace_id if not admin.is_owner else None,
        )

        logger.info(f'Host "{db_host.id}" added by admin "{admin.username}"')

        host = BaseHost.model_validate(db_host)
        asyncio.create_task(notification.create_host(host, admin.username))

        await host_manager.add_host(db, db_host)

        return host

    async def modify_host(
        self, db: AsyncSession, host_id: int, modified_host: CreateHost, admin: AdminDetails
    ) -> BaseHost:
        await self.validate_subscription_templates(db, modified_host, admin)
        await self.validate_ds_host(db, modified_host, host_id, admin=admin)

        if modified_host.inbound_tag and not is_oc_destination_inbound_tag(modified_host.inbound_tag):
            await self.check_host_inbound_tags([modified_host.inbound_tag])

        db_host = await self.get_validated_host(db, host_id, admin=admin)

        if is_oc_destination_inbound_tag(db_host.inbound_tag):
            if modified_host.inbound_tag and modified_host.inbound_tag != db_host.inbound_tag:
                await self.raise_error("OC destination host inbound tag cannot be changed", 400, db=db)
            preserved = CreateHost.model_validate(db_host)
            preserved.remark = modified_host.remark
            preserved.is_disabled = modified_host.is_disabled
            modified_host = preserved
            await update_oc_destination_host_display_name(db, db_host, modified_host.remark)

        db_host = await modify_host(db=db, db_host=db_host, modified_host=modified_host)

        logger.info(f'Host "{db_host.id}" modified by admin "{admin.username}"')

        host = BaseHost.model_validate(db_host)
        asyncio.create_task(notification.modify_host(host, admin.username))

        tenant_id, workspace_id = await self._resource_scope(db, admin)
        db_hosts = await get_hosts(db=db, tenant_id=tenant_id, workspace_id=workspace_id)
        dependents = [
            h
            for h in db_hosts
            if ((h.transport_settings or {}).get("xhttp_settings") or {}).get("download_settings") == host_id
        ]
        await host_manager.add_hosts(db, [db_host, *dependents])

        return host

    async def remove_host(self, db: AsyncSession, host_id: int, admin: AdminDetails):
        db_host = await self.get_validated_host(db, host_id, admin=admin)
        await remove_host(db, db_host)
        logger.info(f'Host "{db_host.id}" deleted by admin "{admin.username}"')

        host = BaseHost.model_validate(db_host)

        asyncio.create_task(notification.remove_host(host, admin.username))

        await host_manager.remove_host(host.id)

    async def modify_hosts(
        self, db: AsyncSession, modified_hosts: list[CreateHost], admin: AdminDetails
    ) -> list[BaseHost]:
        for host in modified_hosts:
            await self.validate_subscription_templates(db, host, admin)
            await self.validate_ds_host(db, host, host.id, admin=admin)

            old_host: ProxyHost | None = None
            tenant_id, workspace_id = await self._resource_scope(db, admin)
            if host.id is not None:
                old_host = await get_host_by_id(
                    db,
                    host.id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )

            if old_host is None:
                await create_host(
                    db,
                    host,
                    tenant_id=tenant_id if not admin.is_owner else None,
                    workspace_id=workspace_id if not admin.is_owner else None,
                )
            else:
                if is_oc_destination_inbound_tag(old_host.inbound_tag):
                    if host.inbound_tag and host.inbound_tag != old_host.inbound_tag:
                        await self.raise_error("OC destination host inbound tag cannot be changed", 400, db=db)
                    preserved = CreateHost.model_validate(old_host)
                    preserved.remark = host.remark
                    preserved.is_disabled = host.is_disabled
                    host = preserved
                    await update_oc_destination_host_display_name(db, old_host, host.remark)
                await modify_host(db, old_host, host)

        tenant_id, workspace_id = await self._resource_scope(db, admin)
        db_hosts = await get_hosts(db=db, tenant_id=tenant_id, workspace_id=workspace_id)
        await host_manager.add_hosts(db, db_hosts)

        logger.info(f'Host\'s has been modified by admin "{admin.username}"')

        asyncio.create_task(notification.modify_hosts(admin.username))

        return db_hosts

    async def bulk_remove_hosts(
        self, db: AsyncSession, bulk_hosts: BulkHostSelection, admin: AdminDetails
    ) -> RemoveHostsResponse:
        """Remove multiple hosts by ID"""
        ids_list = list(bulk_hosts.ids)
        tenant_id, workspace_id = await self._resource_scope(db, admin)
        db_hosts = await get_hosts(
            db,
            HostListQuery(ids=ids_list, limit=len(ids_list)),
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )

        found_ids = {h.id for h in db_hosts}
        missing = set(ids_list) - found_ids
        if missing:
            await self.raise_error(message="Host not found", code=404)

        host_ids = [h.id for h in db_hosts]

        # Batch delete using CRUD function
        await remove_hosts(db, host_ids)

        # Update host manager and notify
        for db_host in db_hosts:
            logger.info(f'Host "{db_host.id}" deleted by admin "{admin.username}"')
            host = BaseHost.model_validate(db_host)
            asyncio.create_task(notification.remove_host(host, admin.username))
            await host_manager.remove_host(host.id)

        return RemoveHostsResponse(hosts=[str(h.id) for h in db_hosts], count=len(db_hosts))

    @staticmethod
    def _build_bulk_action_response(hosts: list[ProxyHost]) -> BulkHostsActionResponse:
        host_ids = [str(host.id) for host in hosts if host.id is not None]
        return BulkHostsActionResponse(hosts=host_ids, count=len(host_ids))

    async def bulk_set_hosts_disabled(
        self,
        db: AsyncSession,
        bulk_hosts: BulkHostSelection,
        admin: AdminDetails,
        *,
        is_disabled: bool,
    ) -> BulkHostsActionResponse:
        ids_list = list(bulk_hosts.ids)
        tenant_id, workspace_id = await self._resource_scope(db, admin)
        db_hosts = await get_hosts(
            db,
            HostListQuery(ids=ids_list, limit=len(ids_list)),
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )

        found_ids = {h.id for h in db_hosts}
        missing = set(ids_list) - found_ids
        if missing:
            await self.raise_error(message="Host not found", code=404)

        hosts_to_update = [db_host for db_host in db_hosts if db_host.is_disabled != is_disabled]

        for db_host in hosts_to_update:
            db_host.is_disabled = is_disabled

        await db.commit()

        for db_host in hosts_to_update:
            await db.refresh(db_host)
            host = BaseHost.model_validate(db_host)
            asyncio.create_task(notification.modify_host(host, admin.username))
            await host_manager.add_host(db, db_host)
            logger.info(
                f'Host "{db_host.id}" bulk {"disabled" if is_disabled else "enabled"} by admin "{admin.username}"'
            )

        return self._build_bulk_action_response(hosts_to_update)
