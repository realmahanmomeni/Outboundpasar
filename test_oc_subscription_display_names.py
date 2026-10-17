"""OC subscription remark naming: Host display vs presentation template."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.base as db_base
import app.db.models  # noqa: F401
import app.db.models_oc  # noqa: F401
from app.db import GetDB
from app.db.base import Base
from app.db.crud.workspace import create_workspace_for_administrator
from app.db.models import (
    Admin,
    AdminRole,
    Group,
    ProxyInbound,
    Settings,
    Tenant,
    TenantStatus,
    User,
    UserStatus,
)
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCPanelDestinationHost

pytestmark = pytest.mark.asyncio(loop_scope="function")
from app.services.oc_host_display import DEFAULT_OC_HOST_DISPLAY_TEMPLATE, resolve_oc_subscription_remark_source
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import replace_link_remark
from app.services.oc_user_subscription_hosts import collect_oc_subscription_links_for_user


def _customer_link(uuid_val: str, remark: str, host: str = "customer.example") -> str:
    return f"vless://{uuid_val}@{host}:8443?encryption=none&security=tls#{remark}"


def _last_synced_configs_for_destinations(dest_rows) -> list[str]:
    """
    Build mapping ``last_synced_configs`` the way runtime subscription expects.

    When discovery catalog matching did not set ``oc_source_config_id``, the sync gate
    accepts the stable destination fingerprint (see ``destination_config_allowed_by_sync``).
    """
    synced: list[str] = []
    for row in dest_rows:
        payload = row.source_payload if isinstance(row.source_payload, dict) else {}
        sid = str(payload.get("oc_source_config_id") or "").strip()
        synced.append(sid or row.destination_config_id)
    return synced


def _presentation_status() -> str:
    return "vless://disc@x:1?encryption=none#📡Status|✅|📆(∞ day)|(0 B-∞)📊"


def _presentation_user() -> str:
    return "vless://disc@x:1?encryption=none#👤v1.7|pg"


@pytest_asyncio.fixture(autouse=True)
async def _isolated_sqlite_for_display_names():
    """Private in-memory DB; avoids shared PostgreSQL and asyncio loop cross-talk."""
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
                    is_owner=False,
                    permissions={},
                    limits={},
                    features={},
                    access={},
                    hwid={},
                )
            )
        global_settings = (
            await session.execute(select(Settings).where(Settings.workspace_id.is_(None)).limit(1))
        ).scalar_one_or_none()
        if global_settings is None:
            session.add(
                Settings(
                    telegram={},
                    webhook={},
                    notification_settings={},
                    notification_enable={},
                    subscription={"rules": []},
                    hwid={},
                    general={},
                    tenant_id=None,
                    workspace_id=None,
                )
            )
        await session.commit()
    try:
        yield factory
    finally:
        patcher.stop()
        from tests.pytest_db_guard import assert_safe_to_dispose_engine

        assert_safe_to_dispose_engine(engine.url)
        await engine.dispose()


async def _admin_for_tests(db) -> Admin:
    tenant = Tenant(name=f"dn_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    admin = Admin(
        username=f"dn_{uuid.uuid4().hex[:6]}",
        hashed_password="x",
        role_id=role.id,
        tenant_id=tenant.id,
    )
    db.add(admin)
    await db.flush()
    await create_workspace_for_administrator(db, admin)
    await db.refresh(admin)
    return admin


def _active_panel_connection():
    return patch(
        "app.services.oc_subscription_runtime.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    )


async def test_host_display_name_overrides_default_template():
    customer = _customer_link("cust-uuid", "🇸🇪|CDN")
    upstream = [customer]
    catalog_id = "HTTP-UP"

    async with GetDB() as db:
        admin = await _admin_for_tests(db)
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="sp", purchaser_identity="b", name="P")
        db.add(panel)
        await db.flush()
        tag = f"oc_{panel.id}_{catalog_id}"
        db.add(ProxyInbound(tag=tag))
        await db.flush()
        db.add(
            OCPanelConfig(
                panel_id=panel.id,
                source_config_id=catalog_id,
                source_name="HTTP-UP",
                virtual_inbound_tag=tag,
            )
        )
        await db.flush()
        await reconcile_panel_destination_hosts(db, panel, upstream)
        dest = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalar_one()
        dest.display_name = "🇸🇪 CDN"
        assert dest.display_name_template == DEFAULT_OC_HOST_DISPLAY_TEMPLATE
        dest_tag = dest.virtual_inbound_tag
        inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == dest_tag))).scalar_one()
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        from app.db.models_oc import OCUserMapping

        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=_last_synced_configs_for_destinations([dest]),
                external_subscription_url="https://203.0.113.10/sub/synthetic",
            )
        )
        await db.commit()
        uid = user.id

    body = "\n".join([_presentation_status(), _presentation_user(), customer])
    with _active_panel_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links",
        new=AsyncMock(return_value=body.split("\n")),
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(
                db, uid, [dest_tag], {"USERNAME": "bob"}, user_tenant_id=None
            )
    assert len(links) == 1
    remark = unquote(links[0].split("#", 1)[-1])
    assert "📡Status" not in remark
    assert "👤v1.7" not in remark
    assert remark == "🇸🇪 CDN"
    assert "cust-uuid" in links[0]


async def test_custom_template_still_renders_variables():
    rendered = resolve_oc_subscription_remark_source(
        display_name="🇸🇪 CDN",
        display_name_template="Flag {USERNAME}",
    )
    assert rendered == "Flag {USERNAME}"
    out = rendered.format_map({"USERNAME": "alice"})
    assert out == "Flag alice"


async def test_credentials_unchanged_when_remark_changes():
    raw = _customer_link("fixed-uuid-1234", "old-remark")
    new = replace_link_remark(raw, "🇸🇪 CDN")
    assert new.startswith("vless://fixed-uuid-1234@")
    assert "old-remark" not in new
    assert "🇸🇪" in new or "%F0" in new


async def test_three_configs_by_source_id_not_order():
    specs = [
        ("HTTP-UP", "🇸🇪 CDN", "c1", "🇸🇪|CDN"),
        ("Shadowsocks TCP", "🇸🇪 DIRECT", "c2", "🇸🇪|DIRECT"),
        ("VLESS XHHTT", "🇸🇪 XHTTP", "c3", "🇸🇪|XHTTP"),
    ]
    customer_links = [
        _customer_link(uuid_val, remark, host=f"{uuid_val}.example")
        for _, _, uuid_val, remark in specs
    ]
    upstream_for_hosts = customer_links
    upstream_for_fetch = [_presentation_status(), _presentation_user(), *customer_links]

    async with GetDB() as db:
        admin = await _admin_for_tests(db)
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(integration_id=intg.id, source_panel_id="sp", purchaser_identity="b", name="P")
        db.add(panel)
        await db.flush()
        for cid, _name, _u, _r in specs:
            tag = f"oc_{panel.id}_{cid}"
            db.add(ProxyInbound(tag=tag))
            await db.flush()
            db.add(
                OCPanelConfig(
                    panel_id=panel.id,
                    source_config_id=cid,
                    source_name=cid,
                    virtual_inbound_tag=tag,
                )
            )
        await db.flush()
        await reconcile_panel_destination_hosts(db, panel, upstream_for_hosts)
        dest_rows = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalars().all()
        tags = []
        for row in dest_rows:
            payload = row.source_payload if isinstance(row.source_payload, dict) else {}
            sid = payload.get("oc_source_config_id")
            raw = str(payload.get("subscription_link") or "")
            for cid, display, uuid_val, _r in specs:
                if cid == sid or uuid_val in raw:
                    row.display_name = display
                    break
            tags.append(row.virtual_inbound_tag)
        inbounds = []
        for t in tags:
            inbounds.append((await db.execute(select(ProxyInbound).where(ProxyInbound.tag == t))).scalar_one())
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=inbounds)
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        from app.db.models_oc import OCUserMapping

        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=_last_synced_configs_for_destinations(dest_rows),
                external_subscription_url="https://203.0.113.11/sub/synthetic",
            )
        )
        await db.commit()
        uid = user.id

    body = "\n".join(upstream_for_fetch)
    with _active_panel_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links",
        new=AsyncMock(return_value=body.split("\n")),
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(db, uid, tags, {}, user_tenant_id=None)

    assert len(links) == 3
    for cid, display, uuid_val, _r in specs:
        matched = [l for l in links if uuid_val in l]
        assert len(matched) == 1
        remark = unquote(matched[0].split("#", 1)[-1])
        assert "📡Status" not in remark
        assert remark == display


async def main():
    await test_host_display_name_overrides_default_template()
    await test_custom_template_still_renders_variables()
    await test_credentials_unchanged_when_remark_changes()
    await test_three_configs_by_source_id_not_order()
    print("test_oc_subscription_display_names: OK")


if __name__ == "__main__":
    asyncio.run(main())
