import base64
import random
import secrets
from collections import defaultdict
from datetime import UTC, datetime as dt, timedelta

from jdatetime import date as jd

from app.core.hosts import host_manager
from app.db.crud.wireguard import pick_peer_ip_for_inbound
from app.db.models import UserStatus
from app.models.status_emojis import STATUS_EMOJIS
from app.models.subscription import SubscriptionInboundData
from app.models.user import UsersResponseWithInbounds
from app.services.subscription_scope import (
    resolve_user_workspace_id,
    subscription_client_templates_for_workspace,
    subscription_settings_for_user,
    subscription_xray_templates_for_workspace,
)
from app.subscription.config_cache import get_sub_config, make_sub_config_key, put_sub_config
from app.utils.system import readable_size

from . import (
    ClashConfiguration,
    ClashMetaConfiguration,
    OutlineConfiguration,
    SingBoxConfiguration,
    StandardLinks,
    WireGuardConfiguration,
    XrayConfiguration,
)

SERVER_IP = "127.0.0.1"
SERVER_IPV6 = "[::1]"


def _build_subscription_config(
    config_format: str,
    client_templates: dict[str, str],
) -> (
    StandardLinks
    | XrayConfiguration
    | SingBoxConfiguration
    | ClashConfiguration
    | ClashMetaConfiguration
    | OutlineConfiguration
    | WireGuardConfiguration
    | None
):
    common_kwargs = {
        "user_agent_template_content": client_templates["USER_AGENT_TEMPLATE"],
        "grpc_user_agent_template_content": client_templates["GRPC_USER_AGENT_TEMPLATE"],
    }

    if config_format == "links":
        return StandardLinks(**common_kwargs)
    if config_format == "clash":
        return ClashConfiguration(
            clash_template_content=client_templates["CLASH_SUBSCRIPTION_TEMPLATE"],
            **common_kwargs,
        )
    if config_format == "clash_meta":
        return ClashMetaConfiguration(
            clash_template_content=client_templates["CLASH_SUBSCRIPTION_TEMPLATE"],
            **common_kwargs,
        )
    if config_format == "sing_box":
        return SingBoxConfiguration(
            singbox_template_content=client_templates["SINGBOX_SUBSCRIPTION_TEMPLATE"],
            **common_kwargs,
        )
    if config_format == "outline":
        return OutlineConfiguration()
    if config_format == "wireguard":
        return WireGuardConfiguration()
    if config_format == "xray":
        return XrayConfiguration(
            xray_template_content=client_templates["XRAY_SUBSCRIPTION_TEMPLATE"],
            **common_kwargs,
        )
    return None


async def generate_subscription(
    user: UsersResponseWithInbounds,
    config_format: str,
    as_base64: bool,
    randomize_order: bool = False,
    user_agent: str = "",
) -> str | bytes:
    from app.db import GetDB
    from app.services.oc_subscription_runtime import (
        resolve_user_tenant_id,
        runtime_oc_subscription_cache_fingerprint,
    )

    runtime_oc_fingerprint: tuple = ()
    workspace_id: int | None = None
    async with GetDB() as db:
        user_tenant_id = await resolve_user_tenant_id(db, user.id, getattr(user, "admin_id", None))
        runtime_oc_fingerprint = await runtime_oc_subscription_cache_fingerprint(db, user.id, user_tenant_id)
        workspace_id = resolve_user_workspace_id(user)
        client_templates = await subscription_client_templates_for_workspace(db, workspace_id)
        xray_template_overrides = (
            await subscription_xray_templates_for_workspace(db, workspace_id) if config_format == "xray" else None
        )
        sub_settings = await subscription_settings_for_user(db, user)

    cache_key = make_sub_config_key(user, config_format, as_base64, randomize_order, runtime_oc_fingerprint)
    cached = get_sub_config(cache_key)
    if cached is not None:
        return cached

    conf = _build_subscription_config(config_format, client_templates)
    if conf is None:
        raise ValueError(f'Unsupported format "{config_format}"')
    custom_variables = get_effective_custom_variables(user, sub_settings.custom_variables)
    format_variables = setup_format_variables(user, sub_settings.custom_variables)

    config = await process_inbounds_and_tags(
        user,
        format_variables,
        conf,
        client_templates,
        xray_template_overrides=xray_template_overrides,
        randomize_order=randomize_order,
        custom_variables=custom_variables,
    )

    if config_format not in ("links", "links_base64"):
        from app.services.oc_upstream_subscription import append_oc_upstream_subscriptions

        config = await append_oc_upstream_subscriptions(
            config,
            user.id,
            getattr(user, "admin_id", None),
            config_format,
            as_base64=as_base64,
            user_agent=user_agent,
        )

    if as_base64 and not isinstance(config, bytes):
        config = base64.b64encode(config.encode()).decode()

    put_sub_config(cache_key, config)
    return config


def format_time_left(seconds_left: int) -> str:
    if not seconds_left or seconds_left <= 0:
        return "∞"

    minutes, _ = divmod(seconds_left, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    months, days = divmod(days, 30)

    result = []
    if months:
        result.append(f"{int(months)}m")
    if days:
        result.append(f"{int(days)}d")
    if hours and (days < 7):
        result.append(f"{int(hours)}h")
    if minutes and not (months or days):
        result.append(f"{int(minutes)}m")
    return " ".join(result)


def _custom_variable_parts(custom_variable) -> tuple[str | None, str]:
    if isinstance(custom_variable, dict):
        key = custom_variable.get("key")
        value = custom_variable.get("value", "")
    else:
        key = getattr(custom_variable, "key", None)
        value = getattr(custom_variable, "value", "")
    return (str(key) if key else None), str(value or "")


def get_effective_custom_variables(
    user: UsersResponseWithInbounds, custom_variables: list | tuple | None = None
) -> list:
    variables = list(custom_variables or [])
    admin_variables = getattr(getattr(user, "admin", None), "custom_variables", None) or []
    variables.extend(admin_variables)
    return variables


def apply_custom_format_variables(format_variables: dict, custom_variables: list | tuple | None = None) -> dict:
    if not custom_variables:
        return format_variables

    custom_keys = {key for key, _ in (_custom_variable_parts(variable) for variable in custom_variables) if key}
    base_variables = defaultdict(
        lambda: "<missing>",
        {key: value for key, value in format_variables.items() if key not in custom_keys},
    )

    for variable in custom_variables:
        key, raw_value = _custom_variable_parts(variable)
        if not key:
            continue
        try:
            format_variables[key] = raw_value.format_map(base_variables)
        except (ValueError, KeyError):
            format_variables[key] = raw_value

    return format_variables


def _format_dynamic_value(value, format_variables: dict):
    if isinstance(value, str):
        try:
            return value.format_map(format_variables)
        except (ValueError, KeyError):
            return value
    if isinstance(value, list):
        return [_format_dynamic_value(item, format_variables) for item in value]
    if isinstance(value, dict):
        return {key: _format_dynamic_value(item, format_variables) for key, item in value.items()}
    return value


def setup_format_variables(user: UsersResponseWithInbounds, custom_variables: list | tuple | None = None) -> dict:
    custom_variables = get_effective_custom_variables(user, custom_variables)
    user_status = user.status
    expire = user.expire
    on_hold_expire_duration = user.on_hold_expire_duration
    now = dt.now(UTC)

    admin_username = ""
    if admin_data := user.admin:
        admin_username = admin_data.username

    if user_status != UserStatus.on_hold:
        if expire is not None:
            seconds_left = (expire - now).total_seconds()
            expire_date_obj = expire.date()
            expire_date = expire_date_obj.strftime("%Y-%m-%d")
            jalali_expire_date = jd.fromgregorian(
                year=expire_date_obj.year, month=expire_date_obj.month, day=expire_date_obj.day
            ).strftime("%Y-%m-%d")
            if now < expire:
                days_left = (expire - now).days + 1
                time_left = format_time_left(seconds_left)
            else:
                days_left = "0"
                time_left = "0"

        else:
            days_left = "∞"
            time_left = "∞"
            expire_date = "∞"
            jalali_expire_date = "∞"
    else:
        if on_hold_expire_duration:
            days_left = timedelta(seconds=on_hold_expire_duration).days
            time_left = format_time_left(on_hold_expire_duration)
            expire_date = "-"
            jalali_expire_date = "-"
        else:
            days_left = "∞"
            time_left = "∞"
            expire_date = "∞"
            jalali_expire_date = "∞"

    if user.data_limit:
        data_limit = readable_size(user.data_limit)
        data_left = user.data_limit - user.used_traffic
        usage_Percentage = round((user.used_traffic / user.data_limit) * 100.0, 2)

        data_left = max(data_left, 0)
        data_left = readable_size(data_left)
    else:
        data_limit = "∞"
        data_left = "∞"
        usage_Percentage = "∞"

    status_emoji = STATUS_EMOJIS.get(user.status.value)

    format_variables = defaultdict(
        lambda: "<missing>",
        {
            "SERVER_IP": SERVER_IP,
            "SERVER_IPV6": SERVER_IPV6,
            "USERNAME": user.username,
            "DATA_USAGE": readable_size(user.used_traffic),
            "DATA_LIMIT": data_limit,
            "DATA_LEFT": data_left,
            "DAYS_LEFT": days_left,
            "EXPIRE_DATE": expire_date,
            "JALALI_EXPIRE_DATE": jalali_expire_date,
            "TIME_LEFT": time_left,
            "STATUS_EMOJI": status_emoji,
            "USAGE_PERCENTAGE": usage_Percentage,
            "ADMIN_USERNAME": admin_username,
        },
    )

    return apply_custom_format_variables(format_variables, custom_variables)


async def filter_hosts(hosts: list[SubscriptionInboundData], user_status: UserStatus) -> list[SubscriptionInboundData]:
    return [host for host in hosts if not host.status or user_status in host.status]


async def resolve_oc_virtual_hosts(
    oc_tags: list[str],
    user_status: UserStatus,
    proxies: dict,
) -> list[SubscriptionInboundData]:
    """
    Resolve OC virtual inbounds from the database for the given authorized tags.
    Respects source_missing, locally_hidden, and host is_disabled flags.
    """
    if not oc_tags:
        return []

    from app.db import GetDB
    from app.db.models import ProxyHost
    from app.db.models_oc import OCPanelConfig
    from app.models.host import BaseHost
    from app.core.hosts import _prepare_subscription_inbound_data
    from sqlalchemy import or_, select

    oc_hosts: list[SubscriptionInboundData] = []
    async with GetDB() as db:
        stmt = (
            select(ProxyHost, OCPanelConfig)
            .join(OCPanelConfig, ProxyHost.inbound_tag == OCPanelConfig.virtual_inbound_tag)
            .where(
                OCPanelConfig.virtual_inbound_tag.in_(oc_tags),
                OCPanelConfig.source_missing.is_(False),
                OCPanelConfig.locally_hidden.is_(False),
                or_(ProxyHost.is_disabled.is_(False), ProxyHost.is_disabled.is_(None)),
            )
            .order_by(ProxyHost.priority.asc(), ProxyHost.id.asc())
        )
        result = await db.execute(stmt)
        rows = result.all()

        for host, config in rows:
            if host.status and user_status not in host.status:
                continue

            host_base = BaseHost.model_validate(host)
            sub_data = await _prepare_subscription_inbound_data(host_base, oc_config=config, proxies=proxies)
            if sub_data:
                oc_hosts.append(sub_data)

    return oc_hosts


async def process_host(
    inbound: SubscriptionInboundData,
    format_variables: dict,
    inbounds: list[str],
    proxies: dict,
    custom_variables: list | tuple | None = None,
) -> None | tuple[SubscriptionInboundData, dict]:
    """
    Process host data for subscription generation.
    Now only does random selection and user-specific formatting!
    All merging and data preparation is done in hosts.py.
    """

    if inbound.inbound_tag not in inbounds:
        return

    # Get user settings for this protocol
    settings = proxies.get(inbound.protocol)
    if not settings:
        return
    settings = dict(settings)

    # Keep user id accessible for protocol-level dynamic allocation helpers.
    user_id = proxies.get("_user_id")
    if user_id is not None:
        settings["_user_id"] = user_id

    # Each WG interface only gets the user's peer IP from its own subnet.
    if inbound.protocol == "wireguard":
        settings["peer_ips"] = pick_peer_ip_for_inbound(inbound.wireguard_local_address, settings.get("peer_ips") or [])

    # Update format variables
    format_variables.update({"PROTOCOL": inbound.protocol})
    format_variables.update({"TRANSPORT": inbound.network})
    apply_custom_format_variables(format_variables, custom_variables)

    salt = secrets.token_hex(8)

    sni = ""
    if isinstance(inbound.tls_config.sni, list) and inbound.tls_config.sni:
        sni = random.choice(inbound.tls_config.sni)
    sni = sni.replace("*", salt)
    sni = sni.format_map(format_variables) if sni else ""

    req_host = ""
    host_list = inbound.transport_config.host
    if isinstance(host_list, list) and host_list:
        req_host = random.choice(host_list)
    req_host = req_host.replace("*", salt)
    req_host = req_host.format_map(format_variables) if req_host else ""

    address = ""
    if inbound.address:
        address = random.choice(inbound.address).replace("*", salt)

    # Select random port from list
    port = random.choice(inbound.port) if inbound.port else 0

    # Select random Reality short ID if available
    if inbound.tls_config.reality_short_ids:
        reality_sid = random.choice(inbound.tls_config.reality_short_ids)
    else:
        reality_sid = inbound.tls_config.reality_short_id

    # Format path with variables
    path = inbound.transport_config.path.format_map(format_variables) if inbound.transport_config.path else ""

    # Apply use_sni_as_host override
    if inbound.use_sni_as_host and sni:
        req_host = sni

    # Copy only the nested models we mutate so cached host objects stay intact.
    transport_update: dict = {"host": req_host, "path": path}
    if getattr(inbound.transport_config, "request", None):
        transport_update["request"] = _format_dynamic_value(
            inbound.transport_config.request,
            format_variables,
        )
    if getattr(inbound.transport_config, "response", None):
        transport_update["response"] = _format_dynamic_value(
            inbound.transport_config.response,
            format_variables,
        )
    inbound_copy = inbound.model_copy(
        update={
            "tls_config": inbound.tls_config.model_copy(
                update={"sni": sni, "reality_short_id": reality_sid},
            ),
            "transport_config": inbound.transport_config.model_copy(update=transport_update),
            "address": address,
            "port": port,
        }
    )

    return inbound_copy, settings


async def _prepare_download_settings(
    download_data: SubscriptionInboundData,
    format_variables: dict,
    inbounds: list[str],
    proxies: dict,
    client_templates: dict[str, str],
    custom_variables: list | tuple | None,
    conf: StandardLinks
    | XrayConfiguration
    | SingBoxConfiguration
    | ClashConfiguration
    | ClashMetaConfiguration
    | OutlineConfiguration
    | WireGuardConfiguration,
) -> SubscriptionInboundData | dict | None:
    result = await process_host(download_data, format_variables, inbounds, proxies, custom_variables)

    if not result:
        return

    download_copy, _ = result

    if isinstance(download_copy.address, str):
        download_copy.address = download_copy.address.format_map(format_variables)

    if isinstance(conf, StandardLinks):
        xc = XrayConfiguration(
            xray_template_content=client_templates["XRAY_SUBSCRIPTION_TEMPLATE"],
            user_agent_template_content=client_templates["USER_AGENT_TEMPLATE"],
            grpc_user_agent_template_content=client_templates["GRPC_USER_AGENT_TEMPLATE"],
        )
        return xc._download_config(download_copy, link_format=True)

    return download_copy


async def process_inbounds_and_tags(
    user: UsersResponseWithInbounds,
    format_variables: dict,
    conf: StandardLinks
    | XrayConfiguration
    | SingBoxConfiguration
    | ClashConfiguration
    | ClashMetaConfiguration
    | OutlineConfiguration
    | WireGuardConfiguration,
    client_templates: dict[str, str],
    xray_template_overrides: dict[int, str] | None = None,
    randomize_order: bool = False,
    custom_variables: list | tuple | None = None,
) -> str | bytes:
    proxy_settings = user.proxy_settings.dict()
    proxy_settings["_user_id"] = user.id
    hosts = await filter_hosts(list((await host_manager.get_hosts()).values()), user.status)
    user_inbounds = list(user.inbounds or [])
    native_inbound_tags = [t for t in user_inbounds if not (t or "").startswith("oc_")]
    desired_oc_tags = [t for t in user_inbounds if (t or "").startswith("oc_")]
    oc_tags = desired_oc_tags
    oc_raw_links: list[str] = []
    if desired_oc_tags:
        from app.db import GetDB
        from app.services.oc_subscription_runtime import (
            filter_oc_inbound_tags_for_runtime_subscription,
            resolve_user_tenant_id,
        )
        from app.services.oc_user_subscription_hosts import collect_oc_subscription_links_for_user

        async with GetDB() as db:
            user_tenant_id = await resolve_user_tenant_id(db, user.id, getattr(user, "admin_id", None))
            oc_tags = await filter_oc_inbound_tags_for_runtime_subscription(
                db, user.id, desired_oc_tags, user_tenant_id
            )
            if oc_tags:
                oc_raw_links = await collect_oc_subscription_links_for_user(
                    db,
                    user.id,
                    oc_tags,
                    format_variables,
                    user_tenant_id=user_tenant_id,
                )
    if randomize_order and len(hosts) > 1:
        random.shuffle(hosts)

    def _resolve_host_xray_template_content(inbound: SubscriptionInboundData) -> str | None:
        if xray_template_overrides is None:
            return None
        if not isinstance(inbound.subscription_templates, dict):
            return None
        template_id = inbound.subscription_templates.get("xray")
        if not isinstance(template_id, int):
            return None
        return xray_template_overrides.get(template_id)

    for host_data in hosts:
        if (host_data.inbound_tag or "").startswith("oc_"):
            continue
        result = await process_host(host_data, format_variables, native_inbound_tags, proxy_settings, custom_variables)
        if not result:
            continue

        inbound_copy: SubscriptionInboundData
        inbound_copy, settings = result

        # Format remark and address with user variables
        remark = inbound_copy.remark.format_map(format_variables)
        formatted_address = inbound_copy.address.format_map(format_variables)

        download_settings = getattr(inbound_copy.transport_config, "download_settings", None)
        if download_settings:
            if isinstance(download_settings, SubscriptionInboundData):
                processed_download_settings = await _prepare_download_settings(
                    download_settings,
                    format_variables,
                    native_inbound_tags,
                    proxy_settings,
                    client_templates,
                    custom_variables,
                    conf,
                )
            else:
                processed_download_settings = download_settings
            if hasattr(inbound_copy.transport_config, "download_settings"):
                inbound_copy.transport_config.download_settings = processed_download_settings

        if isinstance(conf, XrayConfiguration):
            template_content = _resolve_host_xray_template_content(inbound_copy)
            conf.add(
                remark=remark,
                address=formatted_address,
                inbound=inbound_copy,
                settings=settings,
                template_content=template_content,
            )
        else:
            conf.add(
                remark=remark,
                address=formatted_address,
                inbound=inbound_copy,
                settings=settings,
            )

    from app.subscription.links import StandardLinks

    if isinstance(conf, StandardLinks) and oc_raw_links:
        for link in oc_raw_links:
            conf.add_link(link)
    elif desired_oc_tags and isinstance(conf, StandardLinks):
        from app.utils.logger import get_logger

        get_logger("oc-subscription").info(
            "oc_sub_empty_output user=%s desired_oc_tags=%s filtered_oc_tags=%s native_hosts=%s",
            user.id,
            len(desired_oc_tags),
            len(oc_tags),
            len(native_inbound_tags),
        )

    return conf.render()


def encode_title(text: str) -> str:
    return f"base64:{base64.b64encode(text.encode()).decode()}"
