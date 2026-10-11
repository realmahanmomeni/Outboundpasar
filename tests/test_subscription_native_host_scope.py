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


async def _subscription_lines(user_id: int, *, host_ids: list[int] | None = None) -> str:
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
    return await generate_subscription(validated, "links", False)


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
        info_host_id = info_host.id

    body = await _subscription_lines(uid, host_ids=[info_host_id])
    assert not _shadowsocks_uri_lines(body)


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
