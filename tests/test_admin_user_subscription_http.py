"""HTTP contract tests for GET /api/user/{user_id}/subscription/{client_type}."""

from __future__ import annotations

import base64
import io
import json
import uuid
import zipfile
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool

import app.db.base as db_base
import app.db.models  # noqa: F401
import app.db.models_oc  # noqa: F401
from fastapi import FastAPI
from app.routers.user import router as user_api_router
from app.db import GetDB
from app.db.base import Base
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import (
    Admin,
    AdminRole,
    Group,
    JWT,
    ProxyHost,
    ProxyInbound,
    Settings,
    System,
    Tenant,
    TenantStatus,
    User,
    UserStatus,
)
from app.db.models_oc import OCIntegration, OCPanel, OCUserMapping
from app.models.admin_role import PermissionScope
from app.core.hosts import host_manager
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import destination_virtual_inbound_tag
from app.subscription.config_cache import clear_sub_config_cache
from app.utils.crypto import generate_wireguard_keypair
from app.utils.jwt import create_admin_token

pytestmark = pytest.mark.asyncio(loop_scope="function")

_EMPTY_SETTINGS = {
    "telegram": {},
    "webhook": {},
    "notification_settings": {},
    "notification_enable": {},
    "subscription": {"rules": []},
    "hwid": {},
    "general": {},
}


def _link(uid: str, host: str, port: int, remark: str) -> str:
    from urllib.parse import quote

    return f"vless://{uid}@{host}:{port}?encryption=none#%s" % quote(remark)


@pytest_asyncio.fixture(autouse=True)
async def _isolated_sqlite_db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    patcher = patch.object(db_base, "SessionLocal", factory)
    patcher.start()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        role = (
            await session.execute(select(AdminRole).where(AdminRole.name == "administrator"))
        ).scalar_one_or_none()
        if role is None:
            session.add(
                AdminRole(
                    name="administrator",
                    is_owner=True,
                    permissions={},
                    limits={},
                    features={},
                    access={},
                    hwid={},
                )
            )
        if (await session.execute(select(Settings).where(Settings.workspace_id.is_(None)).limit(1))).scalar_one_or_none() is None:
            session.add(Settings(**_EMPTY_SETTINGS, tenant_id=None, workspace_id=None))
        if (await session.execute(select(JWT).limit(1))).scalar_one_or_none() is None:
            session.add(JWT(secret_key="test-jwt-secret-key-for-admin-subscription-http"))
        if (await session.execute(select(System).limit(1))).scalar_one_or_none() is None:
            session.add(System())
        await session.commit()
    try:
        yield factory
    finally:
        patcher.stop()
        from tests.pytest_db_guard import assert_safe_to_dispose_engine

        assert_safe_to_dispose_engine(engine.url)
        await engine.dispose()


@pytest.fixture(autouse=True)
def _clear_sub_cache():
    clear_sub_config_cache()
    yield
    clear_sub_config_cache()


@pytest.fixture(autouse=True)
def _empty_host_manager_cache(monkeypatch):
    monkeypatch.setattr(
        "app.subscription.share.host_manager.get_hosts",
        AsyncMock(return_value={}),
    )


@pytest.fixture(autouse=True)
def _minimal_client_templates(monkeypatch):
    templates = {
        "USER_AGENT_TEMPLATE": "{}",
        "GRPC_USER_AGENT_TEMPLATE": "{}",
        # Real default template: a static "proxies: []" string never renders the serializer data.
        "CLASH_SUBSCRIPTION_TEMPLATE": __import__(
            "app.services.oc_subscription_format_materialize", fromlist=["_default_templates"]
        )._default_templates()["CLASH_SUBSCRIPTION_TEMPLATE"],
        "SINGBOX_SUBSCRIPTION_TEMPLATE": '{"outbounds":[]}',
        "XRAY_SUBSCRIPTION_TEMPLATE": "{}",
    }

    async def _templates(*_args, **_kwargs):
        return templates

    async def _xray_templates(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        "app.subscription.share.subscription_client_templates_for_workspace",
        _templates,
    )
    monkeypatch.setattr(
        "app.subscription.share.subscription_xray_templates_for_workspace",
        _xray_templates,
    )


def _active_connection():
    return patch(
        "app.services.oc_subscription_runtime.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    )


async def _tenant_owner_admin(db):
    tenant = Tenant(name=f"http_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    admin = Admin(
        username=f"adm_{uuid.uuid4().hex[:6]}",
        hashed_password="x",
        role_id=role.id,
        tenant_id=tenant.id,
    )
    db.add(admin)
    await db.flush()
    await create_workspace_for_administrator(db, admin)
    await db.refresh(admin)
    from app.db.crud.settings import ensure_workspace_settings

    await ensure_workspace_settings(db, tenant_id=tenant.id, workspace_id=admin.workspace_id)
    return tenant, admin


async def _limited_admin(db, tenant_id: int):
    role = AdminRole(
        name=f"lim_{uuid.uuid4().hex[:8]}",
        is_owner=False,
        permissions={"users": {"read": {"scope": int(PermissionScope.NONE)}}},
        limits={},
        features={},
        access={},
        hwid={},
    )
    db.add(role)
    await db.flush()
    admin = Admin(
        username=f"lim_{uuid.uuid4().hex[:6]}",
        hashed_password="x",
        role_id=role.id,
        tenant_id=tenant_id,
    )
    db.add(admin)
    await db.flush()
    await create_workspace_for_administrator(db, admin)
    await db.refresh(admin)
    return admin


async def _oc_user(db, tenant, admin, links: list[str]) -> int:
    intg = (await db.execute(select(OCIntegration).limit(1))).scalar_one_or_none()
    if intg is None:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
    panel = OCPanel(
        integration_id=intg.id,
        source_panel_id=f"sp_{uuid.uuid4().hex[:8]}",
        purchaser_identity="buyer",
        name="P",
        tenant_id=tenant.id,
        workspace_id=admin.workspace_id,
        sync_status="connected",
    )
    db.add(panel)
    await db.flush()
    await reconcile_panel_destination_hosts(db, panel, links)
    await db.flush()
    from app.db.models_oc import OCPanelDestinationHost

    rows = (
        await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
    ).scalars().all()
    tags = [destination_virtual_inbound_tag(panel.id, r.destination_config_id) for r in rows]
    inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(tags)))).scalars().all()
    group = Group(name="g", inbounds=list(inbounds), tenant_id=tenant.id, workspace_id=admin.workspace_id)
    db.add(group)
    await db.flush()
    user = User(
        username=f"u_{uuid.uuid4().hex[:6]}",
        status=UserStatus.active,
        admin_id=admin.id,
        workspace_id=admin.workspace_id,
        proxy_settings={"vless": {"id": str(uuid.uuid4())}},
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
            last_synced_configs=[r.destination_config_id for r in rows],
            external_subscription_url="https://203.0.113.10/sub/token",
        )
    )
    await db.commit()
    return user.id


@pytest.fixture
def api_client():
    """Mount only the user router to avoid full application lifespan side effects."""
    app = FastAPI()
    app.include_router(user_api_router)
    with TestClient(app) as client:
        yield client


async def _auth_headers(admin: Admin) -> dict[str, str]:
    token = await create_admin_token(admin.id, admin.username)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_subscription_http_all_formats(api_client):
    links = [_link("uid-a", "host-a.example", 443, "A"), _link("uid-b", "host-b.example", 444, "B")]
    async with GetDB() as db:
        tenant, admin = await _tenant_owner_admin(db)
        user_id = await _oc_user(db, tenant, admin, links)

    async def fake_fetch(_mapping):
        return links

    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links",
        new=AsyncMock(side_effect=fake_fetch),
    ), patch(
        "app.services.oc_upstream_subscription.fetch_upstream_subscription_body",
        new=AsyncMock(side_effect=AssertionError("structured upstream fetch must not run")),
    ):
        headers = await _auth_headers(admin)
        base = f"/api/user/{user_id}/subscription"

        r_links = api_client.get(f"{base}/links", headers=headers)
        assert r_links.status_code == 200
        assert "text/plain" in (r_links.headers.get("content-type") or "")
        plain_lines = [ln.strip() for ln in r_links.text.splitlines() if ln.strip()]
        assert len(plain_lines) == 2

        r_b64 = api_client.get(f"{base}/links_base64", headers=headers)
        assert r_b64.status_code == 200
        decoded = base64.b64decode(r_b64.text.strip()).decode()
        assert decoded.splitlines() == r_links.text.splitlines()

        r_xray = api_client.get(f"{base}/xray", headers=headers)
        assert r_xray.status_code == 200
        assert "application/json" in (r_xray.headers.get("content-type") or "")
        xray_data = json.loads(r_xray.text)
        assert isinstance(xray_data, list) and len(xray_data) == 2
        assert all(item.get("outbounds") and item.get("remarks") for item in xray_data)

        r_outline = api_client.get(f"{base}/outline", headers=headers)
        assert r_outline.status_code == 200
        assert "application/json" in (r_outline.headers.get("content-type") or "")
        outline_doc = json.loads(r_outline.text)
        assert outline_doc == {}

        # Classic Clash has no VLESS: it must be a valid empty proxy list, not invalid vless entries.
        r_clash_classic = api_client.get(f"{base}/clash", headers=headers)
        assert r_clash_classic.status_code == 200
        assert "yaml" in (r_clash_classic.headers.get("content-type") or "")
        assert (yaml.safe_load(r_clash_classic.text) or {}).get("proxies") == []

        r_clash = api_client.get(f"{base}/clash_meta", headers=headers)
        assert r_clash.status_code == 200
        assert "yaml" in (r_clash.headers.get("content-type") or "")
        doc = yaml.safe_load(r_clash.text) or {}
        assert isinstance(doc.get("proxies"), list) and len(doc["proxies"]) == 2

        r_sing = api_client.get(f"{base}/sing_box", headers=headers)
        assert r_sing.status_code == 200
        sing = json.loads(r_sing.text)
        assert [o for o in sing["outbounds"] if o.get("type") == "vless"].__len__() == 2

        r_wg = api_client.get(f"{base}/wireguard", headers=headers)
        assert r_wg.status_code == 200
        assert "zip" in (r_wg.headers.get("content-type") or "")
        body = r_wg.content
        assert isinstance(body, bytes) and body[:2] == b"PK"
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            assert zf.namelist() == []


@pytest.mark.asyncio
async def test_admin_subscription_http_auth_and_permissions(api_client):
    links = [_link("uid-z", "host-z.example", 443, "Z")]
    async with GetDB() as db:
        tenant, admin = await _tenant_owner_admin(db)
        user_id = await _oc_user(db, tenant, admin, links)
        limited = await _limited_admin(db, tenant.id)
        await db.commit()

    url = f"/api/user/{user_id}/subscription/links"
    assert api_client.get(url).status_code == 401

    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links",
        new=AsyncMock(return_value=links),
    ):
        lim_headers = await _auth_headers(limited)
        assert api_client.get(url, headers=lim_headers).status_code == 403

        owner_headers = await _auth_headers(admin)
        assert api_client.get(url, headers=owner_headers).status_code == 200

        assert api_client.get(f"/api/user/999999/subscription/links", headers=owner_headers).status_code == 404


@pytest.mark.asyncio
async def test_admin_subscription_native_wireguard_zip_has_conf_members(api_client, monkeypatch):
    client_priv, client_pub = generate_wireguard_keypair()
    _server_priv, server_pub = generate_wireguard_keypair()
    inbound_tag = f"wg_{uuid.uuid4().hex[:8]}"

    async with GetDB() as db:
        tenant, admin = await _tenant_owner_admin(db)
        inbound = ProxyInbound(tag=inbound_tag)
        db.add(inbound)
        await db.flush()
        host = ProxyHost(
            remark="WG HTTP",
            priority=0,
            address={"wg-server.example"},
            port=51820,
            path=None,
            allowinsecure=None,
            status=[],
            alpn=[],
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
        )
        host.inbound = inbound
        db.add(host)
        await db.flush()
        group = Group(
            name="wg_native",
            inbounds=[inbound],
            tenant_id=tenant.id,
            workspace_id=admin.workspace_id,
        )
        db.add(group)
        await db.flush()
        user = User(
            username=f"wg_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={
                "vless": {"id": str(uuid.uuid4())},
                "wireguard": {
                    "private_key": client_priv,
                    "public_key": client_pub,
                    "peer_ips": ["10.66.0.2/32"],
                },
            },
        )
        user.groups = [group]
        db.add(user)
        await db.commit()
        user_id = user.id
        host_id = host.id

    wg_host_data = SubscriptionInboundData(
        remark="WG HTTP",
        inbound_tag=inbound_tag,
        protocol="wireguard",
        address=["wg-server.example"],
        port=[51820],
        network="tcp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(),
        wireguard_public_key=server_pub,
        wireguard_local_address=["10.66.0.1/24"],
        wireguard_allowed_ips=["0.0.0.0/0", "::/0"],
    )
    await host_manager._add_prepared_hosts_local([(host_id, wg_host_data)])
    monkeypatch.setattr(
        "app.subscription.share.host_manager.get_hosts",
        AsyncMock(return_value={host_id: wg_host_data}),
    )

    headers = await _auth_headers(admin)
    r_wg = api_client.get(f"/api/user/{user_id}/subscription/wireguard", headers=headers)
    assert r_wg.status_code == 200
    assert "zip" in (r_wg.headers.get("content-type") or "")
    body = r_wg.content
    assert body[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
        assert len(names) == 1
        assert names[0].endswith(".conf")
        conf_text = zf.read(names[0]).decode()
        assert "[Interface]" in conf_text
        assert "[Peer]" in conf_text
        assert client_priv in conf_text
        assert server_pub in conf_text


@pytest.mark.asyncio
async def test_admin_subscription_oc_wireguard_blocks_panel_zip_leak(api_client):
    links = [_link("wg-u", "wg.example", 51820, "WG")]
    async with GetDB() as db:
        tenant, admin = await _tenant_owner_admin(db)
        user_id = await _oc_user(db, tenant, admin, links)

    leaked = b"PK\x03\x04LEAKED_WIREGUARD_PANEL_ARCHIVE"
    fetch_mock = AsyncMock(return_value=leaked)

    with _active_connection(), patch(
        "app.services.oc_upstream_subscription.fetch_upstream_subscription_body",
        new=fetch_mock,
    ), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links",
        new=AsyncMock(return_value=links),
    ):
        headers = await _auth_headers(admin)
        r_wg = api_client.get(f"/api/user/{user_id}/subscription/wireguard", headers=headers)
        assert r_wg.status_code == 200
        assert leaked not in r_wg.content
        fetch_mock.assert_not_called()
        with zipfile.ZipFile(io.BytesIO(r_wg.content)) as zf:
            assert zf.namelist() == []
