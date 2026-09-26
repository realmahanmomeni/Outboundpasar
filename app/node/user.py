from PasarGuardNodeBridge import create_proxy, create_user
from PasarGuardNodeBridge.common.service_pb2 import User as ProtoUser
from sqlalchemy import and_, func, or_, select

from app.db import AsyncSession
from app.db.models import (
    Admin,
    AdminRole,
    AdminStatus,
    Group,
    ProxyInbound,
    User,
    UserStatus,
    inbounds_groups_association,
    users_groups_association,
)
from app.models.protocol import ProxyProtocol

_ALL_PROXY_PROTOCOLS = frozenset(ProxyProtocol)


def get_panel_xray_identity(user_id: int, panel_id: int) -> str:
    """Generate a deterministic Xray email identity for a purchased panel user."""
    return f"{user_id}_p{panel_id}"


def _bucket_inbounds(inbounds: list[str], active_panel_ids: list[int] | None = None) -> dict[int | None, list[str]]:
    """
    Group inbound tags into native (None) and panel_id (int) buckets.
    Panel tags conventionally start with 'oc_{panel_id}_{config_id}'.
    """
    buckets: dict[int | None, list[str]] = {None: []}
    if active_panel_ids:
        for p in active_panel_ids:
            buckets[p] = []

    for tag in inbounds:
        if tag.startswith("oc_"):
            parts = tag.split("_")
            if len(parts) >= 3 and parts[1].isdigit():
                panel_id = int(parts[1])
                buckets.setdefault(panel_id, []).append(tag)
                continue
        buckets[None].append(tag)
    
    return buckets


def _inbounds_from_loaded_groups(user: User) -> list[str] | None:
    loaded_groups = user.__dict__.get("groups")
    if loaded_groups is None:
        return None

    tags: set[str] = set()
    for group in loaded_groups:
        if group.is_disabled:
            continue

        loaded_inbounds = group.__dict__.get("inbounds")
        if loaded_inbounds is None:
            return None

        for inbound in loaded_inbounds:
            tags.add(inbound.tag)

    return list(tags)


async def serialize_user(user: User, allowed_protocols: frozenset[ProxyProtocol] | None = None) -> list[ProtoUser]:
    from sqlalchemy.ext.asyncio import async_object_session
    from sqlalchemy import select
    from app.db.models_oc import OCUserMapping

    user_settings = user.proxy_settings
    inbounds = None
    status = user.__dict__.get("status")
    if status is None:
        status = await user.awaitable_attrs.status

    if status in (UserStatus.active, UserStatus.on_hold):
        inbounds = _inbounds_from_loaded_groups(user)
        if inbounds is None:
            inbounds = await user.inbounds()

    active_panel_ids = []
    session = async_object_session(user)
    if session is not None:
        active_panel_ids = (await session.execute(
            select(OCUserMapping.panel_id).where(OCUserMapping.user_id == user.id)
        )).scalars().all()

    return _serialize_user_for_node(user.id, user_settings, inbounds, allowed_protocols, active_panel_ids)


def _serialize_user_for_node(
    id: int,
    user_settings: dict,
    inbounds: list[str] | None = None,
    allowed_protocols: frozenset[ProxyProtocol] | None = None,
    active_panel_ids: list[int] | None = None,
) -> list[ProtoUser]:
    allowed_protocols = allowed_protocols or _ALL_PROXY_PROTOCOLS

    proxy_kwargs = {}
    if ProxyProtocol.vmess in allowed_protocols:
        proxy_kwargs["vmess_id"] = user_settings.get("vmess", {}).get("id")
    if ProxyProtocol.vless in allowed_protocols:
        proxy_kwargs["vless_id"] = user_settings.get("vless", {}).get("id")
    if ProxyProtocol.trojan in allowed_protocols:
        proxy_kwargs["trojan_password"] = user_settings.get("trojan", {}).get("password")
    if ProxyProtocol.shadowsocks in allowed_protocols:
        shadowsocks_settings = user_settings.get("shadowsocks", {})
        proxy_kwargs["shadowsocks_password"] = shadowsocks_settings.get("password")
        proxy_kwargs["shadowsocks_method"] = shadowsocks_settings.get("method")
    if ProxyProtocol.wireguard in allowed_protocols:
        wireguard_settings = user_settings.get("wireguard", {})
        proxy_kwargs["wireguard_public_key"] = wireguard_settings.get("public_key")
        proxy_kwargs["wireguard_peer_ips"] = wireguard_settings.get("peer_ips") or []
    if ProxyProtocol.hysteria in allowed_protocols:
        proxy_kwargs["hysteria_auth"] = user_settings.get("hysteria", {}).get("auth")

    inbounds_list = inbounds or []
    buckets = _bucket_inbounds(inbounds_list, active_panel_ids)
    proto_users = []

    for panel_id, bucket_inbounds in buckets.items():
        identity = get_panel_xray_identity(id, panel_id) if panel_id is not None else str(id)
        proto_users.append(
            create_user(
                identity,
                create_proxy(**proxy_kwargs),
                bucket_inbounds,
            )
        )

    return proto_users


async def core_users(
    db: AsyncSession,
    inbound_tags: list[str] | set[str] | None = None,
    allowed_protocols: frozenset[ProxyProtocol] | None = None,
):
    dialect = db.bind.dialect.name
    inbound_tags = list(dict.fromkeys(inbound_tags or []))

    from app.db.models_oc import OCUserMapping
    from sqlalchemy import String

    # Use dialect-specific aggregation and grouping
    if dialect == "postgresql":
        inbound_agg = func.string_agg(ProxyInbound.tag.distinct(), ",").label("inbound_tags")
        panel_agg = func.string_agg(func.cast(OCUserMapping.panel_id, String).distinct(), ",").label("panel_ids")
    else:
        # MySQL and SQLite use group_concat
        inbound_agg = func.group_concat(ProxyInbound.tag.distinct()).label("inbound_tags")
        panel_agg = func.group_concat(OCUserMapping.panel_id.distinct()).label("panel_ids")

    stmt = (
        select(
            User.id,
            User.proxy_settings,
            inbound_agg,
            panel_agg,
        )
        .outerjoin(users_groups_association, User.id == users_groups_association.c.user_id)
        .outerjoin(
            Group,
            and_(
                users_groups_association.c.groups_id == Group.id,
                Group.is_disabled.is_(False),
            ),
        )
        .outerjoin(inbounds_groups_association, Group.id == inbounds_groups_association.c.group_id)
        .outerjoin(
            ProxyInbound,
            and_(
                inbounds_groups_association.c.inbound_id == ProxyInbound.id,
                ProxyInbound.tag.in_(inbound_tags) if inbound_tags else True,
            ),
        )
        .outerjoin(OCUserMapping, User.id == OCUserMapping.user_id)
        # Exclude users whose admin role blocks user sync for the admin's current status.
        .outerjoin(Admin, Admin.id == User.admin_id)
        .outerjoin(AdminRole, AdminRole.id == Admin.role_id)
        .where(User.status.in_([UserStatus.active, UserStatus.on_hold]))
        .where(
            or_(
                Admin.id.is_(None),
                Admin.status.not_in([AdminStatus.limited, AdminStatus.disabled]),
                and_(Admin.status == AdminStatus.limited, AdminRole.disconnect_users_when_limited.is_not(True)),
                and_(Admin.status == AdminStatus.disabled, AdminRole.disconnect_users_when_disabled.is_not(True)),
            )
        )
        .group_by(User.id)
    )

    results = (await db.execute(stmt)).all()
    bridge_users: list = []

    for row in results:
        inbound_tags = row.inbound_tags.split(",") if row.inbound_tags else []
        active_panel_ids = [int(p) for p in row.panel_ids.split(",")] if getattr(row, "panel_ids", None) else []
        
        bridge_users.extend(
            _serialize_user_for_node(
                row.id,
                row.proxy_settings,
                inbound_tags,
                allowed_protocols,
                active_panel_ids,
            )
        )
    return bridge_users


async def serialize_users_for_node(
    users: list[User],
    allowed_protocols: frozenset[ProxyProtocol] | None = None,
) -> list[ProtoUser]:
    """Serialize users for node dispatch."""
    from sqlalchemy.ext.asyncio import async_object_session
    from sqlalchemy import select
    from app.db.models_oc import OCUserMapping

    bridge_users: list = []
    if not users:
        return bridge_users

    session = async_object_session(users[0])
    panel_mappings = {}
    if session is not None:
        user_ids = [u.id for u in users]
        rows = (await session.execute(
            select(OCUserMapping.user_id, OCUserMapping.panel_id).where(OCUserMapping.user_id.in_(user_ids))
        )).all()
        for r in rows:
            panel_mappings.setdefault(r.user_id, []).append(r.panel_id)

    for user in users:
        inbounds_list = []
        if user.status in [UserStatus.active, UserStatus.on_hold]:
            loaded_inbounds = _inbounds_from_loaded_groups(user)
            if loaded_inbounds is None:
                inbounds_list = await user.inbounds()
            else:
                inbounds_list = loaded_inbounds

        active_panel_ids = panel_mappings.get(user.id, [])
        bridge_users.extend(_serialize_user_for_node(user.id, user.proxy_settings, inbounds_list, allowed_protocols, active_panel_ids))

    return bridge_users
