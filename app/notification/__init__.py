import asyncio
from functools import wraps

from app.models.admin import AdminDetails
from app.models.admin_role import AdminRoleResponse
from app.models.api_key import APIKeyResponse
from app.models.core import CoreResponse
from app.models.group import GroupResponse
from app.models.host import BaseHost
from app.models.node import NodeNotification, NodeResponse
from app.models.user import UserNotificationResponse
from app.models.user_template import UserTemplateResponse
from app.notification.delivery_scope import (
    scope_for_actor_username,
    scope_for_admin_subject,
    scope_for_admin_username,
    scope_for_user_event,
)
from app.settings import notification_enable
from app.utils.logger import get_logger

from . import discord as ds, telegram as tg, webhook as wh
from .dedup import claim_notification_slot

logger = get_logger("Notification")


async def _gather_notifications(event_name: str, *aws):
    results = await asyncio.gather(*aws, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.warning(
                "Notification channel failed for %s: %s",
                event_name,
                result,
                exc_info=(type(result), result, result.__traceback__),
            )


def _safe_notification_task(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            if not await claim_notification_slot(func.__name__, args, kwargs):
                return None
            return await func(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning(
                "Notification task %s failed: %s",
                func.__name__,
                err,
                exc_info=(type(err), err, err.__traceback__),
            )

    return wrapper


async def create_api_key(api_key: APIKeyResponse, admin_username: str, by: str):
    scope = await scope_for_admin_username(admin_username)
    if scope and scope.notification_enable.api_key.create:
        ws = scope.workspace_id
        await asyncio.gather(
            ds.create_api_key(api_key, admin_username, by, delivery_workspace_id=ws),
            tg.create_api_key_tg(api_key, admin_username, by, delivery_workspace_id=ws),
        )


async def modify_api_key(api_key: APIKeyResponse, admin_username: str, by: str):
    scope = await scope_for_admin_username(admin_username)
    if scope and scope.notification_enable.api_key.modify:
        ws = scope.workspace_id
        await asyncio.gather(
            ds.modify_api_key(api_key, admin_username, by, delivery_workspace_id=ws),
            tg.modify_api_key_tg(api_key, admin_username, by, delivery_workspace_id=ws),
        )


async def remove_api_key(api_key: APIKeyResponse, admin_username: str, by: str):
    scope = await scope_for_admin_username(admin_username)
    if scope and scope.notification_enable.api_key.delete:
        ws = scope.workspace_id
        await asyncio.gather(
            ds.remove_api_key(api_key, admin_username, by, delivery_workspace_id=ws),
            tg.remove_api_key_tg(api_key, admin_username, by, delivery_workspace_id=ws),
        )


async def create_admin_role(role: AdminRoleResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.admin_role.create:
        ws = scope.workspace_id
        await _gather_notifications(
            "create_admin_role",
            ds.create_admin_role(role, by, delivery_workspace_id=ws),
            tg.create_admin_role(role, by, delivery_workspace_id=ws),
        )


async def modify_admin_role(role: AdminRoleResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.admin_role.modify:
        ws = scope.workspace_id
        await _gather_notifications(
            "modify_admin_role",
            ds.modify_admin_role(role, by, delivery_workspace_id=ws),
            tg.modify_admin_role(role, by, delivery_workspace_id=ws),
        )


async def remove_admin_role(role: AdminRoleResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.admin_role.delete:
        ws = scope.workspace_id
        await _gather_notifications(
            "remove_admin_role",
            ds.remove_admin_role(role, by, delivery_workspace_id=ws),
            tg.remove_admin_role(role, by, delivery_workspace_id=ws),
        )


async def create_host(host: BaseHost, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.host.create:
        ws = scope.workspace_id
        await _gather_notifications(
            "create_host",
            ds.create_host(host, by, delivery_workspace_id=ws),
            tg.create_host(host, by, delivery_workspace_id=ws),
        )


async def modify_host(host: BaseHost, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.host.modify:
        ws = scope.workspace_id
        await _gather_notifications(
            "modify_host",
            ds.modify_host(host, by, delivery_workspace_id=ws),
            tg.modify_host(host, by, delivery_workspace_id=ws),
        )


async def remove_host(host: BaseHost, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.host.delete:
        ws = scope.workspace_id
        await _gather_notifications(
            "remove_host",
            ds.remove_host(host, by, delivery_workspace_id=ws),
            tg.remove_host(host, by, delivery_workspace_id=ws),
        )


async def modify_hosts(by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.host.modify_hosts:
        ws = scope.workspace_id
        await _gather_notifications(
            "modify_hosts",
            ds.modify_hosts(by, delivery_workspace_id=ws),
            tg.modify_hosts(by, delivery_workspace_id=ws),
        )


async def create_user_template(user: UserTemplateResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.user_template.create:
        ws = scope.workspace_id
        await _gather_notifications(
            "create_user_template",
            ds.create_user_template(user, by, delivery_workspace_id=ws),
            tg.create_user_template(user, by, delivery_workspace_id=ws),
        )


async def modify_user_template(user: UserTemplateResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.user_template.modify:
        ws = scope.workspace_id
        await _gather_notifications(
            "modify_user_template",
            ds.modify_user_template(user, by, delivery_workspace_id=ws),
            tg.modify_user_template(user, by, delivery_workspace_id=ws),
        )


async def remove_user_template(name: str, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.user_template.delete:
        ws = scope.workspace_id
        await _gather_notifications(
            "remove_user_template",
            ds.remove_user_template(name, by, delivery_workspace_id=ws),
            tg.remove_user_template(name, by, delivery_workspace_id=ws),
        )


async def create_node(node: NodeResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.node.create:
        ws = scope.workspace_id
        await _gather_notifications(
            "create_node",
            ds.create_node(node, by, delivery_workspace_id=ws),
            tg.create_node(node, by, delivery_workspace_id=ws),
        )


async def modify_node(node: NodeResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.node.modify:
        ws = scope.workspace_id
        await _gather_notifications(
            "modify_node",
            ds.modify_node(node, by, delivery_workspace_id=ws),
            tg.modify_node(node, by, delivery_workspace_id=ws),
        )


async def remove_node(node: NodeResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.node.delete:
        ws = scope.workspace_id
        await _gather_notifications(
            "remove_node",
            ds.remove_node(node, by, delivery_workspace_id=ws),
            tg.remove_node(node, by, delivery_workspace_id=ws),
        )


async def connect_node(node: NodeNotification):
    scope = await scope_for_actor_username("system")
    if scope and scope.notification_enable.node.connect:
        ws = scope.workspace_id
        await _gather_notifications(
            "connect_node",
            ds.connect_node(node, delivery_workspace_id=ws),
            tg.connect_node(node, delivery_workspace_id=ws),
        )


async def recovered_node(node: NodeNotification):
    scope = await scope_for_actor_username("system")
    if scope and scope.notification_enable.node.recovered:
        ws = scope.workspace_id
        await _gather_notifications(
            "recovered_node",
            ds.recovered_node(node, delivery_workspace_id=ws),
            tg.recovered_node(node, delivery_workspace_id=ws),
        )


async def error_node(node: NodeNotification):
    scope = await scope_for_actor_username("system")
    if scope and scope.notification_enable.node.error:
        ws = scope.workspace_id
        await _gather_notifications(
            "error_node",
            ds.error_node(node, delivery_workspace_id=ws),
            tg.error_node(node, delivery_workspace_id=ws),
        )


async def limited_node(node: NodeNotification, data_limit: int, used_traffic: int):
    scope = await scope_for_actor_username("system")
    if scope and scope.notification_enable.node.limited:
        ws = scope.workspace_id
        await _gather_notifications(
            "limited_node",
            ds.limited_node(node, data_limit, used_traffic, delivery_workspace_id=ws),
            tg.limited_node(node, data_limit, used_traffic, delivery_workspace_id=ws),
        )


async def reset_node_usage(node: NodeResponse, by: str, uplink: int, downlink: int):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.node.reset_usage:
        ws = scope.workspace_id
        await _gather_notifications(
            "reset_node_usage",
            ds.reset_node_usage(node, by, uplink, downlink, delivery_workspace_id=ws),
            tg.reset_node_usage(node, by, uplink, downlink, delivery_workspace_id=ws),
        )


async def create_group(group: GroupResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.group.create:
        ws = scope.workspace_id
        await _gather_notifications(
            "create_group",
            ds.create_group(group, by, delivery_workspace_id=ws),
            tg.create_group(group, by, delivery_workspace_id=ws),
        )


async def modify_group(group: GroupResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.group.modify:
        ws = scope.workspace_id
        await _gather_notifications(
            "modify_group",
            ds.modify_group(group, by, delivery_workspace_id=ws),
            tg.modify_group(group, by, delivery_workspace_id=ws),
        )


async def remove_group(group_id: int, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.group.delete:
        ws = scope.workspace_id
        await _gather_notifications(
            "remove_group",
            ds.remove_group(group_id, by, delivery_workspace_id=ws),
            tg.remove_group(group_id, by, delivery_workspace_id=ws),
        )


async def create_admin(admin: AdminDetails, by: str):
    scope = await scope_for_admin_subject(admin)
    if scope and scope.notification_enable.admin.create:
        ws = scope.workspace_id
        await _gather_notifications(
            "create_admin",
            ds.create_admin(admin, by, delivery_workspace_id=ws),
            tg.create_admin(admin, by, delivery_workspace_id=ws),
        )


async def modify_admin(admin: AdminDetails, by: str):
    scope = await scope_for_admin_subject(admin)
    if scope and scope.notification_enable.admin.modify:
        ws = scope.workspace_id
        await _gather_notifications(
            "modify_admin",
            ds.modify_admin(admin, by, delivery_workspace_id=ws),
            tg.modify_admin(admin, by, delivery_workspace_id=ws),
        )


async def remove_admin(username: str, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.admin.delete:
        ws = scope.workspace_id
        await _gather_notifications(
            "remove_admin",
            ds.remove_admin(username, by, delivery_workspace_id=ws),
            tg.remove_admin(username, by, delivery_workspace_id=ws),
        )


async def admin_usage_reset(admin: AdminDetails, by: str):
    scope = await scope_for_admin_subject(admin)
    if scope and scope.notification_enable.admin.reset_usage:
        ws = scope.workspace_id
        await _gather_notifications(
            "admin_usage_reset",
            ds.admin_reset_usage(admin, by, delivery_workspace_id=ws),
            tg.admin_reset_usage(admin, by, delivery_workspace_id=ws),
        )


async def admin_usage_limit_reached(admin: AdminDetails, usage_percentage: int, threshold: int):
    if (await notification_enable()).admin.usage_limit_warning:
        await _gather_notifications(
            "admin_usage_limit_reached",
            ds.admin_usage_limit_reached(admin, usage_percentage, threshold),
            tg.admin_usage_limit_reached(admin, usage_percentage, threshold),
        )


async def admin_login(username: str, password: str, client_ip: str, success: bool):
    if (await notification_enable()).admin.login:
        await _gather_notifications(
            "admin_login",
            ds.admin_login(username, password, client_ip, success),
            tg.admin_login(username, password, client_ip, success),
        )


async def user_status_change(user: UserNotificationResponse, by: AdminDetails):
    scope = await scope_for_user_event(user, by)
    if scope and scope.notification_enable.user.status_change:
        ws = scope.workspace_id
        await _gather_notifications(
            "user_status_change",
            ds.user_status_change(user, by.username, delivery_workspace_id=ws),
            tg.user_status_change(user, by.username, delivery_workspace_id=ws),
            wh.status_change(user, delivery_workspace_id=ws),
        )


async def create_user(user: UserNotificationResponse, by: AdminDetails):
    scope = await scope_for_user_event(user, by)
    if scope and scope.notification_enable.user.create:
        ws = scope.workspace_id
        await _gather_notifications(
            "create_user",
            ds.create_user(user, by.username, delivery_workspace_id=ws),
            tg.create_user(user, by.username, delivery_workspace_id=ws),
            wh.notify(wh.UserCreated(username=user.username, user=user, by=by), delivery_workspace_id=ws),
        )


async def modify_user(user: UserNotificationResponse, by: AdminDetails):
    scope = await scope_for_user_event(user, by)
    if scope and scope.notification_enable.user.modify:
        ws = scope.workspace_id
        await _gather_notifications(
            "modify_user",
            ds.modify_user(user, by.username, delivery_workspace_id=ws),
            tg.modify_user(user, by.username, delivery_workspace_id=ws),
            wh.notify(wh.UserUpdated(username=user.username, user=user, by=by), delivery_workspace_id=ws),
        )


async def remove_user(user: UserNotificationResponse, by: AdminDetails):
    scope = await scope_for_user_event(user, by)
    if scope and scope.notification_enable.user.delete:
        ws = scope.workspace_id
        await _gather_notifications(
            "remove_user",
            ds.remove_user(user, by.username, delivery_workspace_id=ws),
            tg.remove_user(user, by.username, delivery_workspace_id=ws),
            wh.notify(wh.UserDeleted(username=user.username, user=user, by=by), delivery_workspace_id=ws),
        )


async def reset_user_data_usage(user: UserNotificationResponse, by: AdminDetails):
    scope = await scope_for_user_event(user, by)
    if scope and scope.notification_enable.user.reset_data_usage:
        ws = scope.workspace_id
        await _gather_notifications(
            "reset_user_data_usage",
            ds.reset_user_data_usage(user, by.username, delivery_workspace_id=ws),
            tg.reset_user_data_usage(user, by.username, delivery_workspace_id=ws),
            wh.notify(wh.UserDataUsageReset(username=user.username, user=user, by=by), delivery_workspace_id=ws),
        )


async def user_data_reset_by_next(user: UserNotificationResponse, by: AdminDetails):
    scope = await scope_for_user_event(user, by)
    if scope and scope.notification_enable.user.data_reset_by_next:
        ws = scope.workspace_id
        await _gather_notifications(
            "user_data_reset_by_next",
            ds.user_data_reset_by_next(user, by.username, delivery_workspace_id=ws),
            tg.user_data_reset_by_next(user, by.username, delivery_workspace_id=ws),
            wh.notify(wh.UserDataResetByNext(username=user.username, user=user, by=by), delivery_workspace_id=ws),
        )


async def user_subscription_revoked(user: UserNotificationResponse, by: AdminDetails):
    scope = await scope_for_user_event(user, by)
    if scope and scope.notification_enable.user.subscription_revoked:
        ws = scope.workspace_id
        await _gather_notifications(
            "user_subscription_revoked",
            ds.user_subscription_revoked(user, by.username, delivery_workspace_id=ws),
            tg.user_subscription_revoked(user, by.username, delivery_workspace_id=ws),
            wh.notify(
                wh.UserSubscriptionRevoked(username=user.username, user=user, by=by),
                delivery_workspace_id=ws,
            ),
        )


async def create_core(core: CoreResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.core.create:
        ws = scope.workspace_id
        await _gather_notifications(
            "create_core",
            ds.create_core(core, by, delivery_workspace_id=ws),
            tg.create_core(core, by, delivery_workspace_id=ws),
        )


async def modify_core(core: CoreResponse, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.core.modify:
        ws = scope.workspace_id
        await _gather_notifications(
            "modify_core",
            ds.modify_core(core, by, delivery_workspace_id=ws),
            tg.modify_core(core, by, delivery_workspace_id=ws),
        )


async def remove_core(core_id: int, by: str):
    scope = await scope_for_actor_username(by)
    if scope and scope.notification_enable.core.delete:
        ws = scope.workspace_id
        await _gather_notifications(
            "remove_core",
            ds.remove_core(core_id, by, delivery_workspace_id=ws),
            tg.remove_core(core_id, by, delivery_workspace_id=ws),
        )


for _task_name in (
    "create_api_key",
    "modify_api_key",
    "remove_api_key",
    "create_admin_role",
    "modify_admin_role",
    "remove_admin_role",
    "create_host",
    "modify_host",
    "remove_host",
    "modify_hosts",
    "create_user_template",
    "modify_user_template",
    "remove_user_template",
    "create_node",
    "modify_node",
    "remove_node",
    "connect_node",
    "recovered_node",
    "error_node",
    "limited_node",
    "reset_node_usage",
    "create_group",
    "modify_group",
    "remove_group",
    "create_admin",
    "modify_admin",
    "remove_admin",
    "admin_usage_reset",
    "admin_usage_limit_reached",
    "admin_login",
    "user_status_change",
    "create_user",
    "modify_user",
    "remove_user",
    "reset_user_data_usage",
    "user_data_reset_by_next",
    "user_subscription_revoked",
    "create_core",
    "modify_core",
    "remove_core",
):
    globals()[_task_name] = _safe_notification_task(globals()[_task_name])

del _task_name
