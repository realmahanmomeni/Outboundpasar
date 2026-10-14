"""Native subscription output must respect workspace/tenant scope and skip informational hosts."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.hosts import host_manager
from app.db import GetDB
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import (
    Admin,
    AdminRole,
    Group,
    ProxyHost,
    ProxyInbound,
    Tenant,
    TenantStatus,
    User,
    UserStatus,
)
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost, OCUserMapping
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.services.oc_host_display import DEFAULT_OC_HOST_DISPLAY_TEMPLATE
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import destination_virtual_inbound_tag
from app.subscription.config_cache import clear_sub_config_cache
import base64

from app.services.oc_share_link import (
    is_importable_configuration_link,
    is_native_informational_display_link,
    parse_share_link,
)
from app.subscription.share import generate_subscription


@pytest.fixture(autouse=True)
async def _reset_host_manager_between_tests():
    async with host_manager._lock:
        host_manager._hosts = {}
    clear_sub_config_cache()
    yield
    async with host_manager._lock:
        host_manager._hosts = {}
    clear_sub_config_cache()


_SS_PASSWORD = "x" * 22


def _customer_link(uid: str, remark: str) -> str:
    return f"vless://{uid}@customer.example:8443?encryption=none&security=tls#{remark}"


def _shadowsocks_uri_lines(body: str) -> list[str]:
    return [line for line in body.strip().splitlines() if line.startswith("ss://")]


def _metadata_lines(body: str) -> list[str]:
    return [
        line.strip()
        for line in body.strip().splitlines()
        if line.strip() and not is_importable_configuration_link(line.strip())
    ]


def _native_info_display_lines(body: str) -> list[str]:
    return [line.strip() for line in body.strip().splitlines() if is_native_informational_display_link(line.strip())]


def _assert_no_connectable_proxy_lines(body: str) -> None:
    for line in body.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        assert not is_importable_configuration_link(stripped), stripped


async def _tenant_admin(db, name: str | None = None):
    tenant = Tenant(name=f"subscope_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    admin = Admin(
        username=name or f"adm_{uuid.uuid4().hex[:6]}",
        hashed_password="x",
        role_id=role.id,
        tenant_id=tenant.id,
    )
    db.add(admin)
    await db.flush()
    await create_workspace_for_administrator(db, admin)
    await db.refresh(admin)
    return tenant, admin


async def _tenant_admin_without_workspace(db, name: str | None = None):
    tenant = Tenant(name=f"nows_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    admin = Admin(
        username=name or f"adm_{uuid.uuid4().hex[:6]}",
        hashed_password="x",
        role_id=role.id,
        tenant_id=tenant.id,
    )
    db.add(admin)
    await db.flush()
    await db.refresh(admin)
    return tenant, admin


def _proxy_host(
    *,
    remark: str,
    inbound: ProxyInbound,
    tenant_id: int,
    workspace_id: int,
    address: set[str],
    port: int | None,
) -> ProxyHost:
    host = ProxyHost(
        remark=remark,
        priority=0,
        address=address,
        port=port,
        path=None,
        status=[],
        alpn=[],
        is_disabled=False,
        allowinsecure=None,
    )
    host.inbound = inbound
    host.tenant_id = tenant_id
    host.workspace_id = workspace_id
    return host


def _shadowsocks_subscription_data(row: ProxyHost) -> SubscriptionInboundData:
    tag = (row.inbound.tag if row.inbound else None) or row.inbound_tag
    address = next(iter(row.address), "127.0.0.1")
    return SubscriptionInboundData(
        remark=row.remark,
        inbound_tag=tag,
        protocol="shadowsocks",
        address=[address],
        port=[row.port or 1080],
        network="tcp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(),
        encryption="chacha20-ietf-poly1305",
    )


async def _subscription_body(
    user_id: int,
    *,
    host_ids: list[int] | None = None,
    config_format: str = "links",
    as_base64: bool = False,
) -> str:
    clear_sub_config_cache()
    sub_op = SubscriptionOperation(OperatorType.API)
    async with GetDB() as db:
        user = (
            await db.execute(
                select(User)
                .where(User.id == user_id)
                .options(selectinload(User.groups).selectinload(Group.inbounds))
            )
        ).scalar_one()
        if host_ids:
            rows = (
                await db.execute(
                    select(ProxyHost)
                    .where(ProxyHost.id.in_(host_ids))
                    .options(selectinload(ProxyHost.inbound))
                )
            ).scalars().all()
            prepared = [(row.id, _shadowsocks_subscription_data(row)) for row in rows]
            await host_manager._add_prepared_hosts_local(prepared)
        validated = await sub_op.validated_user(user)
    raw = await generate_subscription(validated, config_format, as_base64)
    if isinstance(raw, bytes):
        raw = raw.decode()
    if as_base64:
        return base64.b64decode(raw).decode()
    return raw


async def _subscription_lines(user_id: int, *, host_ids: list[int] | None = None) -> str:
    return await _subscription_body(user_id, host_ids=host_ids)


async def _oc_panel(db, tenant, admin, links: list[str]):
    intg = (await db.execute(select(OCIntegration).limit(1))).scalar_one_or_none()
    if intg is None:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
    panel = OCPanel(
        integration_id=intg.id,
        source_panel_id=f"sp_{uuid.uuid4().hex[:8]}",
        purchaser_identity="buyer",
        name="panel",
        tenant_id=tenant.id,
        workspace_id=admin.workspace_id,
        sync_status="connected",
    )
    db.add(panel)
    await db.flush()
    await reconcile_panel_destination_hosts(db, panel, links)
    await db.flush()
    dest_row = (
        await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
    ).scalars().first()
    tag = destination_virtual_inbound_tag(panel.id, dest_row.destination_config_id)
    return panel, tag, dest_row


async def test_cross_workspace_shared_inbound_tag_does_not_leak_panel_hosts():
    shared_tag = f"scope_ss_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant_a, admin_a = await _tenant_admin(db)
        tenant_b, admin_b = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()

        leak_host = _proxy_host(
            remark="Panel 33",
            inbound=inbound,
            tenant_id=tenant_b.id,
            workspace_id=admin_b.workspace_id,
            address={"https://other.example/dashboard"},
            port=None,
        )
        legit_host = _proxy_host(
            remark="My SS node",
            inbound=inbound,
            tenant_id=tenant_a.id,
            workspace_id=admin_a.workspace_id,
            address={"ss-node.example"},
            port=1080,
        )
        db.add_all([leak_host, legit_host])
        await db.flush()

        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin_a.id,
            workspace_id=admin_a.workspace_id,
            proxy_settings={
                "shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD},
                "vless": {"id": str(uuid.uuid4())},
            },
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        host_ids = [leak_host.id, legit_host.id]

    body = await _subscription_lines(uid, host_ids=host_ids)
    assert "Panel 33" not in body
    assert "ss-node.example" in body
    assert _shadowsocks_uri_lines(body)


async def test_informational_template_host_not_emitted_as_shadowsocks():
    shared_tag = f"info_ss_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        info_host = _proxy_host(
            remark=DEFAULT_OC_HOST_DISPLAY_TEMPLATE.strip(),
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"1.1.1.1"},
            port=None,
        )
        db.add(info_host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        uname = user.username
        info_host_id = info_host.id

    body = await _subscription_lines(uid, host_ids=[info_host_id])
    assert not _shadowsocks_uri_lines(body)
    info_lines = _native_info_display_lines(body)
    assert info_lines
    joined = "\n".join(info_lines)
    assert uname in joined
    assert "{USERNAME}" not in joined
    _assert_no_connectable_proxy_lines(body)
    parsed_remarks = []
    for line in info_lines:
        parsed = parse_share_link(line)
        assert parsed is not None
        parsed_remarks.append(parsed.remark)
    assert uname in "\n".join(parsed_remarks)


async def test_oc_links_remain_with_native_info_inbound_on_group():
    customer = _customer_link("cust-1", "cfg")
    shared_tag = f"mix_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, oc_tag, _dest = await _oc_panel(db, tenant, admin, [customer])
        inbound = ProxyInbound(tag=shared_tag)
        oc_inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == oc_tag))).scalar_one()
        db.add(inbound)
        await db.flush()
        info_host = _proxy_host(
            remark="👤{USERNAME}|({DATA_LEFT})",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"127.0.0.1"},
            port=None,
        )
        db.add(info_host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[oc_inbound, inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=["cfg"],
                external_subscription_url="https://1.1.1.1/sub/token",
            )
        )
        await db.commit()
        uid = user.id
        uname = user.username
        info_host_id = info_host.id

    conn = patch(
        "app.services.oc_subscription_runtime.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    )
    fetch = patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=customer),
    )
    with conn, fetch:
        body = await _subscription_lines(uid, host_ids=[info_host_id])
    assert "customer.example" in body
    assert not _shadowsocks_uri_lines(body)
    assert "Panel 33" not in body
    assert uname in body
    assert _native_info_display_lines(body)


async def test_oc_only_group_emits_no_native_shadowsocks():
    customer = _customer_link("only-oc", "cfg")
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, oc_tag, _ = await _oc_panel(db, tenant, admin, [customer])
        oc_inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == oc_tag))).scalar_one()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[oc_inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=["cfg"],
                external_subscription_url="https://1.1.1.1/sub/token",
            )
        )
        await db.commit()
        uid = user.id

    conn = patch(
        "app.services.oc_subscription_runtime.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    )
    fetch = patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=customer),
    )
    with conn, fetch:
        body = await _subscription_lines(uid)
    assert "customer.example" in body
    assert not _shadowsocks_uri_lines(body)


async def test_tenant_scope_excludes_foreign_tenant_with_same_inbound_tag():
    shared_tag = f"tenant_ss_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant_a, admin_a = await _tenant_admin(db)
        tenant_b, admin_b = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        foreign = _proxy_host(
            remark="Foreign tenant host",
            inbound=inbound,
            tenant_id=tenant_b.id,
            workspace_id=admin_b.workspace_id,
            address={"foreign.example"},
            port=1080,
        )
        local = _proxy_host(
            remark="Local tenant host",
            inbound=inbound,
            tenant_id=tenant_a.id,
            workspace_id=admin_a.workspace_id,
            address={"local.example"},
            port=1080,
        )
        db.add_all([foreign, local])
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin_a.id,
            workspace_id=admin_a.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        host_ids = [foreign.id, local.id]

    body = await _subscription_lines(uid, host_ids=host_ids)
    assert "foreign.example" not in body
    assert "local.example" in body


async def test_unresolved_workspace_scope_emits_no_native_hosts():
    shared_tag = f"nows_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin_no_ws = await _tenant_admin_without_workspace(db)
        _, admin_with_ws = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        foreign = _proxy_host(
            remark="Foreign ws host",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin_with_ws.workspace_id,
            address={"foreign-ws.example"},
            port=1080,
        )
        local = _proxy_host(
            remark="Would-be local",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin_with_ws.workspace_id,
            address={"local-ws.example"},
            port=1080,
        )
        db.add_all([foreign, local])
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin_no_ws.id,
            workspace_id=None,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        host_ids = [foreign.id, local.id]

    body = await _subscription_lines(uid, host_ids=host_ids)
    assert not _shadowsocks_uri_lines(body)
    assert "foreign-ws.example" not in body
    assert "local-ws.example" not in body


async def test_unresolved_tenant_scope_emits_no_native_hosts():
    shared_tag = f"notenant_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        host = _proxy_host(
            remark="Any native",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"any.example"},
            port=1080,
        )
        db.add(host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=None,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        host_ids = [host.id]

    body = await _subscription_lines(uid, host_ids=host_ids)
    assert not _shadowsocks_uri_lines(body)
    assert "any.example" not in body


async def test_workspace_resolved_from_admin_db_when_user_workspace_missing():
    shared_tag = f"fallback_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        host = _proxy_host(
            remark="Fallback ws node",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"fallback.example"},
            port=1080,
        )
        db.add(host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=None,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        host_ids = [host.id]

    body = await _subscription_lines(uid, host_ids=host_ids)
    assert "fallback.example" in body
    assert _shadowsocks_uri_lines(body)


async def test_data_left_only_template_remark_not_serialized():
    shared_tag = f"dleft_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        info_host = _proxy_host(
            remark="({DATA_LEFT})",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"1.2.3.4"},
            port=1080,
        )
        db.add(info_host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        info_host_id = info_host.id

    body = await _subscription_lines(uid, host_ids=[info_host_id])
    assert not _shadowsocks_uri_lines(body)
    info_lines = _native_info_display_lines(body)
    assert len(info_lines) == 1
    parsed = parse_share_link(info_lines[0])
    assert parsed is not None
    assert "{DATA_LEFT}" not in parsed.remark
    assert parsed.remark.startswith("(")
    _assert_no_connectable_proxy_lines(body)


async def test_oc_links_remain_when_native_workspace_scope_unresolved():
    customer = _customer_link("scope-oc", "cfg")
    shared_tag = f"ocnows_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, panel_admin = await _tenant_admin(db)
        panel, oc_tag, _ = await _oc_panel(db, tenant, panel_admin, [customer])
        role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin_no_ws = Admin(
            username=f"adm_{uuid.uuid4().hex[:6]}",
            hashed_password="x",
            role_id=role.id,
            tenant_id=tenant.id,
        )
        db.add(admin_no_ws)
        await db.flush()
        oc_inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == oc_tag))).scalar_one()
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        native_host = _proxy_host(
            remark="Should not emit",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=panel_admin.workspace_id,
            address={"native-blocked.example"},
            port=1080,
        )
        db.add(native_host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[oc_inbound, inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin_no_ws.id,
            workspace_id=None,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=["cfg"],
                external_subscription_url="https://1.1.1.1/sub/token",
            )
        )
        await db.commit()
        uid = user.id
        host_ids = [native_host.id]

    conn = patch(
        "app.services.oc_subscription_runtime.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    )
    fetch = patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=customer),
    )
    with conn, fetch:
        body = await _subscription_lines(uid, host_ids=host_ids)
    assert "customer.example" in body
    assert not _shadowsocks_uri_lines(body)
    assert "native-blocked.example" not in body


async def test_native_only_group_connectable_and_informational_hosts():
    shared_tag = f"native_mix_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        info_host = _proxy_host(
            remark="👤{USERNAME}|({DATA_LEFT})",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"127.0.0.1"},
            port=None,
        )
        connectable = _proxy_host(
            remark="Real node",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"ss-node.example"},
            port=1080,
        )
        db.add_all([info_host, connectable])
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        uname = user.username
        host_ids = [info_host.id, connectable.id]

    body = await _subscription_lines(uid, host_ids=host_ids)
    assert uname in body
    assert "ss-node.example" in body
    assert _shadowsocks_uri_lines(body)
    assert _native_info_display_lines(body)
    assert not any("Panel 33" in line for line in body.splitlines())


async def test_disabled_informational_host_not_emitted():
    shared_tag = f"disabled_info_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        info_host = _proxy_host(
            remark="👤{USERNAME}",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"127.0.0.1"},
            port=None,
        )
        info_host.is_disabled = True
        db.add(info_host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        uname = user.username
        host_ids = [info_host.id]

    body = await _subscription_lines(uid, host_ids=host_ids)
    assert uname not in body
    assert not _native_info_display_lines(body)


async def test_foreign_template_host_on_shared_inbound_not_emitted():
    shared_tag = f"foreign_tpl_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant_a, admin_a = await _tenant_admin(db)
        tenant_b, admin_b = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        foreign_info = _proxy_host(
            remark="👤{USERNAME}|Panel leak",
            inbound=inbound,
            tenant_id=tenant_b.id,
            workspace_id=admin_b.workspace_id,
            address={"foreign.example"},
            port=None,
        )
        local_info = _proxy_host(
            remark="👤{USERNAME}|local ok",
            inbound=inbound,
            tenant_id=tenant_a.id,
            workspace_id=admin_a.workspace_id,
            address={"127.0.0.1"},
            port=None,
        )
        db.add_all([foreign_info, local_info])
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin_a.id,
            workspace_id=admin_a.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        uname = user.username
        host_ids = [foreign_info.id, local_info.id]

    body = await _subscription_lines(uid, host_ids=host_ids)
    assert "Panel leak" not in body
    assert not _shadowsocks_uri_lines(body)
    info_lines = _native_info_display_lines(body)
    assert len(info_lines) == 1
    parsed = parse_share_link(info_lines[0])
    assert parsed is not None
    assert "local ok" in parsed.remark
    assert uname in parsed.remark


async def test_informational_host_not_connectable_in_xray_format():
    shared_tag = f"xray_info_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        info_host = _proxy_host(
            remark="👤{USERNAME}|({DATA_LEFT})",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"9.9.9.9"},
            port=1080,
        )
        db.add(info_host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        uname = user.username
        host_ids = [info_host.id]

    clear_sub_config_cache()
    sub_op = SubscriptionOperation(OperatorType.API)
    async with GetDB() as db:
        user = (
            await db.execute(
                select(User)
                .where(User.id == uid)
                .options(selectinload(User.groups).selectinload(Group.inbounds))
            )
        ).scalar_one()
        rows = (
            await db.execute(
                select(ProxyHost)
                .where(ProxyHost.id.in_(host_ids))
                .options(selectinload(ProxyHost.inbound))
            )
        ).scalars().all()
        prepared = [(row.id, _shadowsocks_subscription_data(row)) for row in rows]
        await host_manager._add_prepared_hosts_local(prepared)
        validated = await sub_op.validated_user(user)
    body = await generate_subscription(validated, "xray", False)
    if isinstance(body, bytes):
        body = body.decode()
    assert uname not in body
    assert "9.9.9.9" not in body
    assert _SS_PASSWORD not in body


async def test_allowlisted_info_host_emitted_when_host_manager_cache_empty():
    shared_tag = f"cache_miss_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        info_host = _proxy_host(
            remark="📊 {USERNAME}",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address=set(),
            port=None,
        )
        db.add(info_host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        uname = user.username

    async with host_manager._lock:
        host_manager._hosts = {}
    body = await _subscription_lines(uid)
    assert _native_info_display_lines(body)
    assert uname in body


async def test_links_base64_includes_informational_display_via_public_path():
    shared_tag = f"b64info_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        info_host = _proxy_host(
            remark="📊 {USERNAME}|{DATA_LEFT}",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"127.0.0.1"},
            port=None,
        )
        db.add(info_host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        uname = user.username
        host_ids = [info_host.id]

    decoded = await _subscription_body(uid, host_ids=host_ids, as_base64=True)
    assert uname in decoded
    assert not _shadowsocks_uri_lines(decoded)
    info_lines = _native_info_display_lines(decoded)
    assert len(info_lines) == 1
    assert "foreign-leak-marker" not in decoded


async def test_cross_workspace_shared_tag_informational_and_connectable_isolation():
    shared_tag = f"ws_info_{uuid.uuid4().hex[:8]}"
    foreign_marker = f"foreign_ws_b_{uuid.uuid4().hex[:6]}"
    local_marker = f"local_ws_a_{uuid.uuid4().hex[:6]}"
    async with GetDB() as db:
        tenant_a, admin_a = await _tenant_admin(db)
        tenant_b, admin_b = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        local_info = _proxy_host(
            remark=f"👤{{USERNAME}}|{local_marker}",
            inbound=inbound,
            tenant_id=tenant_a.id,
            workspace_id=admin_a.workspace_id,
            address={"127.0.0.1"},
            port=None,
        )
        foreign_info = _proxy_host(
            remark=f"👤{{USERNAME}}|{foreign_marker}",
            inbound=inbound,
            tenant_id=tenant_b.id,
            workspace_id=admin_b.workspace_id,
            address={"127.0.0.1"},
            port=None,
        )
        foreign_ss = _proxy_host(
            remark="Panel 33",
            inbound=inbound,
            tenant_id=tenant_b.id,
            workspace_id=admin_b.workspace_id,
            address={"leak-ss.example"},
            port=1080,
        )
        db.add_all([local_info, foreign_info, foreign_ss])
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin_a.id,
            workspace_id=admin_a.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        uname = user.username
        host_ids = [local_info.id, foreign_info.id, foreign_ss.id]

    plain = await _subscription_lines(uid, host_ids=host_ids)
    decoded_b64 = await _subscription_body(uid, host_ids=host_ids, as_base64=True)
    for body in (plain, decoded_b64):
        assert foreign_marker not in body
        assert "Panel 33" not in body
        assert "leak-ss.example" not in body
        assert local_marker in body
        assert uname in body
        assert not _shadowsocks_uri_lines(body)
        assert len(_native_info_display_lines(body)) == 1


async def test_multiple_informational_hosts_distinct_display_lines():
    shared_tag = f"multi_info_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        info_a = _proxy_host(
            remark="📊 Info A|{USERNAME}",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address=set(),
            port=None,
        )
        info_b = _proxy_host(
            remark="📊 Info B|{DATA_LEFT}",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address=set(),
            port=None,
        )
        db.add_all([info_a, info_b])
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        uname = user.username
        host_ids = [info_a.id, info_b.id]

    body = await _subscription_lines(uid, host_ids=host_ids)
    lines = _native_info_display_lines(body)
    assert len(lines) == 2
    remarks = {parse_share_link(line).remark for line in lines}
    assert any("Info A" in r and uname in r for r in remarks)
    assert any("Info B" in r for r in remarks)
    assert len(remarks) == 2


async def test_remark_with_data_left_marker_classified_as_informational_not_ss():
    """
    Product convention: ``remark_looks_like_user_template`` treats ``{DATA_LEFT}`` as informational.

    A Host whose remark contains that marker is not serialized as Shadowsocks even with address/port set.
    """
    shared_tag = f"ambig_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        host = _proxy_host(
            remark="Production node {DATA_LEFT}",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"real-node.example"},
            port=443,
        )
        db.add(host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        uid = user.id
        host_ids = [host.id]

    body = await _subscription_lines(uid, host_ids=host_ids)
    assert "real-node.example" not in body
    assert not _shadowsocks_uri_lines(body)
    assert _native_info_display_lines(body)


async def test_informational_rendering_is_per_requesting_user():
    shared_tag = f"peruser_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        inbound = ProxyInbound(tag=shared_tag)
        db.add(inbound)
        await db.flush()
        info_host = _proxy_host(
            remark="👤{USERNAME}",
            inbound=inbound,
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
            address={"127.0.0.1"},
            port=None,
        )
        db.add(info_host)
        await db.flush()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user_a = User(
            username=f"user_a_{uuid.uuid4().hex[:4]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user_b = User(
            username=f"user_b_{uuid.uuid4().hex[:4]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"shadowsocks": {"method": "chacha20-ietf-poly1305", "password": _SS_PASSWORD}},
        )
        user_a.groups = [group]
        user_b.groups = [group]
        db.add_all([user_a, user_b])
        await db.commit()
        uid_a, uid_b = user_a.id, user_b.id
        name_a, name_b = user_a.username, user_b.username
        host_ids = [info_host.id]

    body_a = await _subscription_lines(uid_a, host_ids=host_ids)
    body_b = await _subscription_lines(uid_b, host_ids=host_ids)
    assert name_a in body_a
    assert name_b not in body_a
    assert name_b in body_b
    assert name_a not in body_b
