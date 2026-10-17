"""Group-level OC host selection must constrain every subscription output path."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
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
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost, OCUserMapping
from app.operation import OperatorType
from app.models.settings import ConfigFormat
from app.operation.subscription import SubscriptionOperation
from app.services.oc_destination_runtime import destination_config_allowed_by_sync
from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
from app.services.oc_share_link import destination_virtual_inbound_tag, share_link_config_base
from app.services.oc_upstream_subscription import filter_upstream_subscription_body
from app.services.oc_user_subscription_hosts import (
    build_oc_subscription_links_from_state,
    collect_oc_subscription_links_for_user,
    load_oc_subscription_links_state,
)
from app.subscription.config_cache import clear_sub_config_cache
from app.subscription.share import generate_subscription


def _link(uid: str, host: str, port: int, remark: str) -> str:
    from urllib.parse import quote

    return f"vless://{uid}@{host}:{port}?encryption=none#%s" % quote(remark)


@pytest_asyncio.fixture(autouse=True)
async def _isolated_sqlite_db():
    """Private in-memory DB; never uses ``.env`` PostgreSQL."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    patcher = patch.object(db_base, "SessionLocal", factory)
    patcher.start()
    probe = db_base.GetDB()
    assert str(probe.db.bind.url).startswith("sqlite+aiosqlite://"), "refusing to run against a real DB"
    await probe.db.close()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _empty_settings = {
        "telegram": {},
        "webhook": {},
        "notification_settings": {},
        "notification_enable": {},
        "subscription": {"rules": []},
        "hwid": {},
        "general": {},
    }
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
            session.add(Settings(**_empty_settings, tenant_id=None, workspace_id=None))
        await session.commit()
    try:
        yield factory
    finally:
        patcher.stop()
        from tests.pytest_db_guard import assert_safe_to_dispose_engine

        assert_safe_to_dispose_engine(engine.url)
        await engine.dispose()


async def _tenant_admin(db):
    tenant = Tenant(name=f"gf_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one_or_none()
    if role is None:
        role = AdminRole(
            name="administrator",
            is_owner=False,
            permissions={},
            limits={},
            features={},
            access={},
            hwid={},
        )
        db.add(role)
        await db.flush()
    admin = Admin(
        username=f"gf_{uuid.uuid4().hex[:6]}",
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


async def _panel(db, tenant, admin, links: list[str], *, name: str):
    intg = (await db.execute(select(OCIntegration).limit(1))).scalar_one_or_none()
    if intg is None:
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
    panel = OCPanel(
        integration_id=intg.id,
        source_panel_id=f"sp_{uuid.uuid4().hex[:8]}",
        purchaser_identity="buyer",
        name=name,
        tenant_id=tenant.id,
        workspace_id=admin.workspace_id,
        sync_status="connected",
    )
    db.add(panel)
    await db.flush()
    await reconcile_panel_destination_hosts(db, panel, links)
    await db.flush()
    rows = (
        await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
    ).scalars().all()
    return panel, rows


def _tags(panel, rows) -> list[str]:
    return [destination_virtual_inbound_tag(panel.id, r.destination_config_id) for r in rows]


def _active_connection():
    return patch(
        "app.services.oc_subscription_runtime.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    )


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


def _default_clash_template() -> str:
    from app.services.oc_subscription_format_materialize import _default_templates

    return _default_templates()["CLASH_SUBSCRIPTION_TEMPLATE"]


@pytest.fixture(autouse=True)
def _minimal_client_templates(monkeypatch):
    templates = {
        "USER_AGENT_TEMPLATE": "{}",
        "GRPC_USER_AGENT_TEMPLATE": "{}",
        # Must be the real default: a static "proxies: []" string never renders ``conf`` and would
        # hide every entry added by the native serializer.
        "CLASH_SUBSCRIPTION_TEMPLATE": _default_clash_template(),
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


@pytest.mark.asyncio
async def test_multi_panel_subset_links_only_selected_hosts():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        p40, r40 = await _panel(
            db,
            tenant,
            admin,
            [_link("d40a", "h40a.example", 443, "A40"), _link("d40b", "h40b.example", 443, "B40")],
            name="P40",
        )
        p45, r45 = await _panel(
            db,
            tenant,
            admin,
            [_link("d45a", "h45a.example", 443, "A45"), _link("d45b", "h45b.example", 443, "B45")],
            name="P45",
        )
        r40_sel = next(r for r in r40 if (r.display_name or "") == "A40")
        r45_sel = next(r for r in r45 if (r.display_name or "") == "A45")
        selected = [
            destination_virtual_inbound_tag(p40.id, r40_sel.destination_config_id),
            destination_virtual_inbound_tag(p45.id, r45_sel.destination_config_id),
        ]
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(selected)))).scalars().all()
        group = Group(name="subset", inbounds=list(inbounds), tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
        db.add_all(
            [
                OCUserMapping(
                    user_id=user.id,
                    panel_id=p40.id,
                    external_user_id="ext40",
                    status="active",
                    last_synced_configs=[r40_sel.destination_config_id],
                    external_subscription_url="https://1.1.1.1/sub/p40",
                ),
                OCUserMapping(
                    user_id=user.id,
                    panel_id=p45.id,
                    external_user_id="ext45",
                    status="active",
                    last_synced_configs=[r45_sel.destination_config_id],
                    external_subscription_url="https://1.0.0.1/sub/p45",
                ),
            ]
        )
        await db.commit()
        uid = user.id

    cust = {
        p40.id: [_link("c40a", "h40a.example", 443, "A40"), _link("c40b", "h40b.example", 443, "B40")],
        p45.id: [_link("c45a", "h45a.example", 443, "A45"), _link("c45b", "h45b.example", 443, "B45")],
    }

    async def fake_fetch(mapping):
        return cust[mapping.panel_id]

    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links", new=fake_fetch
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(db, uid, selected, {}, user_tenant_id=None)

    assert len(links) == 2
    bases = {share_link_config_base(link) for link in links}
    assert share_link_config_base(cust[p40.id][0]) in bases
    assert share_link_config_base(cust[p45.id][0]) in bases
    assert share_link_config_base(cust[p40.id][1]) not in bases
    assert share_link_config_base(cust[p45.id][1]) not in bases


@pytest.mark.asyncio
async def test_panel_isolation_same_remark_different_panels():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        shared_remark = "SharedName"
        p1, r1 = await _panel(
            db,
            tenant,
            admin,
            [_link("d1", "p1.example", 443, shared_remark), _link("d1b", "p1b.example", 443, "OnlyP1")],
            name="P1",
        )
        p2, r2 = await _panel(
            db,
            tenant,
            admin,
            [_link("d2", "p2.example", 443, shared_remark), _link("d2b", "p2b.example", 443, "OnlyP2")],
            name="P2",
        )
        selected = [
            destination_virtual_inbound_tag(p1.id, r1[0].destination_config_id),
            destination_virtual_inbound_tag(p2.id, r2[0].destination_config_id),
        ]
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(selected)))).scalars().all()
        group = Group(name="iso", inbounds=list(inbounds), tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
        for panel, row in ((p1, r1[0]), (p2, r2[0])):
            db.add(
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel.id,
                    external_user_id=str(uuid.uuid4()),
                    status="active",
                    last_synced_configs=[row.destination_config_id],
                    external_subscription_url=f"https://1.1.1.{panel.id}/sub/x",
                )
            )
        await db.commit()
        uid = user.id

    cust = {
        p1.id: [_link("c1", "p1.example", 443, shared_remark), _link("c1b", "p1b.example", 443, "OnlyP1")],
        p2.id: [_link("c2", "p2.example", 443, shared_remark), _link("c2b", "p2b.example", 443, "OnlyP2")],
    }

    async def fake_fetch(mapping):
        return cust[mapping.panel_id]

    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links", new=fake_fetch
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(db, uid, selected, {}, user_tenant_id=None)

    bases = {share_link_config_base(link) for link in links}
    assert share_link_config_base(cust[p1.id][0]) in bases
    assert share_link_config_base(cust[p2.id][0]) in bases
    assert share_link_config_base(cust[p1.id][1]) not in bases
    assert share_link_config_base(cust[p2.id][1]) not in bases


def test_filter_clash_upstream_drops_unselected_proxies():
    allowed = [_link("c1", "sel.example", 443, "Selected")]
    upstream = (
        "proxies:\n"
        "  - {name: Selected, server: sel.example, port: 443, type: vless, uuid: c1}\n"
        "  - {name: Other, server: other.example, port: 443, type: vless, uuid: other-id}\n"
    )
    filtered = filter_upstream_subscription_body(upstream, "clash", allowed)
    assert "sel.example" in filtered
    assert "other.example" not in filtered


def test_filter_clash_does_not_broaden_on_duplicate_endpoint_without_matching_uuid():
    """Same server:port and remark on two configs; only the selected UUID may pass."""
    allowed = [_link("user-a", "shared.example", 443, "SameLabel")]
    upstream = (
        "proxies:\n"
        "  - {name: SameLabel, server: shared.example, port: 443, type: vless, uuid: user-a}\n"
        "  - {name: SameLabel, server: shared.example, port: 443, type: vless, uuid: user-b}\n"
    )
    filtered = filter_upstream_subscription_body(upstream, "clash", allowed)
    assert "uuid: user-a" in filtered or "user-a" in filtered
    assert "user-b" not in filtered


def test_filter_links_upstream_keeps_exact_config_bases_only():
    allowed = [_link("only", "a.example", 443, "A")]
    upstream = "\n".join(
        [
            _link("only", "a.example", 443, "A"),
            _link("other", "b.example", 443, "B"),
        ]
    )
    filtered = filter_upstream_subscription_body(upstream, "links", allowed)
    lines = [line for line in filtered.splitlines() if line.strip()]
    assert len(lines) == 1
    assert share_link_config_base(lines[0]) == share_link_config_base(allowed[0])


def test_filter_xray_upstream_matches_vless_id_add_port():
    allowed = [_link("xray-uid", "x.example", 8443, "X")]
    upstream = json.dumps(
        [
            {"protocol": "vless", "id": "xray-uid", "add": "x.example", "port": 8443, "ps": "X"},
            {"protocol": "vless", "id": "other", "add": "x.example", "port": 8443, "ps": "X"},
        ]
    )
    filtered = filter_upstream_subscription_body(upstream, "xray", allowed)
    data = json.loads(filtered)
    assert len(data) == 1
    assert data[0]["id"] == "xray-uid"


def test_filter_sing_box_upstream_matches_vless_uuid_server_port():
    allowed = [_link("sb-uid", "s.example", 443, "S")]
    upstream = json.dumps(
        {
            "outbounds": [
                {"type": "vless", "uuid": "sb-uid", "server": "s.example", "server_port": 443},
                {"type": "vless", "uuid": "sb-other", "server": "s.example", "server_port": 443},
            ],
            "endpoints": [],
        }
    )
    filtered = filter_upstream_subscription_body(upstream, "sing_box", allowed)
    doc = json.loads(filtered)
    assert len(doc["outbounds"]) == 1
    assert doc["outbounds"][0]["uuid"] == "sb-uid"


def test_filter_outline_returns_empty_without_exact_mapping():
    allowed = [_link("o", "o.example", 443, "O")]
    assert filter_upstream_subscription_body('{"server":"o.example"}', "outline", allowed) == "{}"


def test_filter_wireguard_never_passes_panel_zip_through():
    allowed = [_link("wg-user", "wg.example", 51820, "WG")]
    panel_zip = b"PK\x03\x04unselected-wireguard-config"
    assert filter_upstream_subscription_body(panel_zip, "wireguard", allowed) == b""


def test_filter_trojan_clash_matches_by_share_link_config_base_not_remark():
    allowed = ["trojan://secret-pass@tro.example:443#Selected"]
    upstream = (
        "proxies:\n"
        "  - {name: Selected, server: tro.example, port: 443, type: trojan, password: secret-pass}\n"
        "  - {name: Selected, server: tro.example, port: 443, type: trojan, password: other-pass}\n"
    )
    filtered = filter_upstream_subscription_body(upstream, "clash", allowed)
    assert "secret-pass" in filtered or "tro.example" in filtered
    assert "other-pass" not in filtered


@pytest.mark.asyncio
async def test_wireguard_subscription_skips_oc_upstream_fetch_when_restricted():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        p, rows = await _panel(
            db,
            tenant,
            admin,
            [_link("d40a", "h40a.example", 443, "Sel40"), _link("d40b", "h40b.example", 443, "Other40")],
            name="P40",
        )
        r_sel = next(r for r in rows if (r.display_name or "") == "Sel40")
        selected = [destination_virtual_inbound_tag(p.id, r_sel.destination_config_id)]
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(selected)))).scalars().all()
        group = Group(name="wg", inbounds=list(inbounds), tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
                panel_id=p.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=[r_sel.destination_config_id],
                external_subscription_url="https://1.1.1.1/sub/wg",
            )
        )
        await db.commit()
        uid = user.id

    fetch_mock = AsyncMock(return_value=b"PK\x03\x04LEAKED_WIREGUARD_PANEL_ARCHIVE")
    sub_op = SubscriptionOperation(OperatorType.API)
    with _active_connection(), patch(
        "app.services.oc_upstream_subscription.fetch_upstream_subscription_body",
        new=fetch_mock,
    ), patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=_link("c40a", "h40a.example", 443, "Sel40")),
    ):
        async with GetDB() as db:
            u = (
                await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(selectinload(User.groups).selectinload(Group.inbounds))
                )
            ).scalar_one()
            validated = await sub_op.validated_user(u)
        body = await generate_subscription(validated, "wireguard", False)

    fetch_mock.assert_not_called()
    if isinstance(body, bytes):
        assert b"LEAKED_WIREGUARD_PANEL_ARCHIVE" not in body
    else:
        assert "LEAKED_WIREGUARD_PANEL_ARCHIVE" not in body


@pytest.mark.asyncio
async def test_clash_subscription_respects_group_subset_not_full_upstream():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        p40, r40 = await _panel(
            db,
            tenant,
            admin,
            [_link("d40a", "h40a.example", 443, "Sel40"), _link("d40b", "h40b.example", 443, "Other40")],
            name="P40",
        )
        p45, r45 = await _panel(
            db,
            tenant,
            admin,
            [_link("d45a", "h45a.example", 443, "Sel45"), _link("d45b", "h45b.example", 443, "Other45")],
            name="P45",
        )
        r40_sel = next(r for r in r40 if (r.display_name or "") == "Sel40")
        r45_sel = next(r for r in r45 if (r.display_name or "") == "Sel45")
        selected = [
            destination_virtual_inbound_tag(p40.id, r40_sel.destination_config_id),
            destination_virtual_inbound_tag(p45.id, r45_sel.destination_config_id),
        ]
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(selected)))).scalars().all()
        group = Group(name="clash_subset", inbounds=list(inbounds), tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
        urls = {p40.id: "https://1.1.1.1/sub/p40", p45.id: "https://1.0.0.1/sub/p45"}
        for panel, row in ((p40, r40_sel), (p45, r45_sel)):
            db.add(
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel.id,
                    external_user_id=str(uuid.uuid4()),
                    status="active",
                    last_synced_configs=[row.destination_config_id],
                    external_subscription_url=urls[panel.id],
                )
            )
        await db.commit()
        uid = user.id

    clash_by_panel = {
        p40.id: (
            "proxies:\n"
            "  - {name: Sel40, server: h40a.example, port: 443, type: vless, uuid: c40a}\n"
            "  - {name: Other40, server: h40b.example, port: 443, type: vless, uuid: c40b}\n"
        ),
        p45.id: (
            "proxies:\n"
            "  - {name: Sel45, server: h45a.example, port: 443, type: vless, uuid: c45a}\n"
            "  - {name: Other45, server: h45b.example, port: 443, type: vless, uuid: c45b}\n"
        ),
    }
    links_by_panel = {
        p40.id: "\n".join(
            [_link("c40a", "h40a.example", 443, "Sel40"), _link("c40b", "h40b.example", 443, "Other40")]
        ),
        p45.id: "\n".join(
            [_link("c45a", "h45a.example", 443, "Sel45"), _link("c45b", "h45b.example", 443, "Other45")]
        ),
    }

    async def fake_fetch(url, fmt, **kwargs):
        for pid, base in urls.items():
            if base.split("/sub")[0] in url:
                if fmt in ("links", "links_base64"):
                    return links_by_panel[pid]
                return clash_by_panel[pid]
        raise AssertionError(url)

    sub_op = SubscriptionOperation(OperatorType.API)
    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts.fetch_upstream_subscription_body",
        new=AsyncMock(side_effect=fake_fetch),
    ), patch(
        "app.services.oc_upstream_subscription.fetch_upstream_subscription_body",
        new=AsyncMock(side_effect=fake_fetch),
    ):
        async with GetDB() as db:
            u = (
                await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(selectinload(User.groups).selectinload(Group.inbounds))
                )
            ).scalar_one()
            validated = await sub_op.validated_user(u)
        # VLESS exists only in Clash Meta; classic ``clash`` deliberately excludes it (see
        # test_classic_clash_excludes_vless_like_native in test_oc_subscription_structured_pipeline).
        clash = await generate_subscription(validated, "clash_meta", False)

    assert "h40b.example" not in clash
    assert "h45b.example" not in clash
    assert "h40a.example" in clash
    assert "h45a.example" in clash


def test_destination_sync_denies_unselected_when_sync_restricted():
    assert not destination_config_allowed_by_sync(
        oc_source_config_id="cfg-b",
        panel_id=40,
        destination_config_id="fp-b",
        synced={"fp-a"},
    )


@pytest.mark.asyncio
async def test_empty_group_oc_selection_yields_no_upstream_merge():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        p, rows = await _panel(db, tenant, admin, [_link("d", "h.example", 443, "One")], name="P")
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=admin.workspace_id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        db.add(user)
        await db.flush()
        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=p.id,
                external_user_id="ext",
                status="active",
                last_synced_configs=[rows[0].destination_config_id],
                external_subscription_url="https://1.1.1.1/sub/x",
            )
        )
        await db.commit()
        uid = user.id

    upstream = "proxies:\n  - {name: One, server: h.example, port: 443, type: vless}\n"
    sub_op = SubscriptionOperation(OperatorType.API)
    with _active_connection(), patch(
        "app.services.oc_upstream_subscription.fetch_upstream_subscription_body",
        new=AsyncMock(return_value=upstream),
    ):
        async with GetDB() as db:
            u = await db.get(User, uid)
            validated = await sub_op.validated_user(u)
        clash = await generate_subscription(validated, "clash", False)

    assert "h.example" not in clash


def _link_identities(links: list[str]) -> set[tuple[str, str, int]]:
    from urllib.parse import urlparse

    ids: set[tuple[str, str, int]] = set()
    for link in links:
        base = share_link_config_base(link)
        if not base:
            continue
        parsed = urlparse(base.split("#")[0])
        host = (parsed.hostname or "").lower()
        port = parsed.port or 0
        cred = (parsed.username or "").lower()
        if host and port and cred:
            ids.add((cred, host, port))
    return ids


def _endpoint_identity_fingerprints(links: list[str]) -> set[tuple[str, str, int, str]]:
    """Protocol, host, port, and a non-reversible credential fingerprint (no secrets in assertions)."""
    import hashlib
    from urllib.parse import unquote, urlparse

    from app.services.oc_share_link import parse_share_link

    out: set[tuple[str, str, int, str]] = set()
    for link in links:
        parsed = parse_share_link(link)
        if parsed is None or not parsed.address or not parsed.port:
            continue
        cred = ""
        if parsed.protocol == "vless":
            uri = urlparse(share_link_config_base(link))
            cred = unquote(uri.username or "")
        elif parsed.protocol == "vmess":
            cred = str(parsed.extra.get("id") or "")
        if not cred:
            continue
        cred_fp = hashlib.sha256(cred.encode()).hexdigest()[:12]
        out.add((parsed.protocol, parsed.address.lower(), int(parsed.port), cred_fp))
    return out


def _xray_identity_fingerprints(items: list) -> set[tuple[str, str, int, str]]:
    from tests.oc_format_identity import xray_identities

    return {i for i in xray_identities(items) if i[0] in ("vless", "vmess")}


def _singbox_identity_fingerprints(doc: dict) -> set[tuple[str, str, int, str]]:
    from tests.oc_format_identity import singbox_identities

    return {i for i in singbox_identities(doc) if i[0] in ("vless", "vmess")}


def _clash_identity_fingerprints(doc: dict) -> set[tuple[str, str, int, str]]:
    from tests.oc_format_identity import clash_identities

    return {i for i in clash_identities(doc) if i[0] in ("vless", "vmess")}


@pytest.mark.asyncio
async def test_structured_formats_match_web_links_without_upstream_structured_fetch():
    """Xray/sing-box/clash must mirror web links; no seller structured subscription fetch."""
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panels = []
        all_rows = []
        link_defs = []
        for idx, (name, hosts) in enumerate(
            (
                ("P1", [("u1a", "h1a.example", 443), ("u1b", "h1b.example", 444), ("u1c", "h1c.example", 445)]),
                ("P2", [("u2a", "h2a.example", 443), ("u2b", "h2b.example", 444), ("u2c", "h2c.example", 445)]),
                ("P3", [("u3a", "h3a.example", 443), ("u3b", "h3b.example", 444)]),
            )
        ):
            links = [_link(uid, host, port, f"Sel{idx}{i}") for i, (uid, host, port) in enumerate(hosts)]
            panel, rows = await _panel(db, tenant, admin, links, name=name)
            panels.append(panel)
            all_rows.append(rows)
            link_defs.append(links)
        selected = []
        synced = []
        for panel, rows, panel_links in zip(panels, all_rows, link_defs, strict=True):
            for row, link in zip(rows, panel_links, strict=True):
                selected.append(destination_virtual_inbound_tag(panel.id, row.destination_config_id))
                synced.append((panel.id, row.destination_config_id, link))
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(selected)))).scalars().all()
        group = Group(name="parity8", inbounds=list(inbounds), tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
        configs_by_panel: dict[int, list[str]] = {}
        for panel_id, dest_id, _link_str in synced:
            configs_by_panel.setdefault(panel_id, []).append(dest_id)
        for panel_id, dest_ids in configs_by_panel.items():
            db.add(
                OCUserMapping(
                    user_id=user.id,
                    panel_id=panel_id,
                    external_user_id=f"ext_{panel_id}",
                    status="active",
                    last_synced_configs=dest_ids,
                    external_subscription_url=f"https://203.0.113.{panel_id % 250}/sub/p{panel_id}",
                )
            )
        await db.commit()
        uid = user.id

    cust = {panel.id: "\n".join(links) for panel, links in zip(panels, link_defs, strict=True)}

    async def fake_fetch(mapping):
        return cust[mapping.panel_id].split("\n")

    structured_fetch = AsyncMock(side_effect=AssertionError("must not fetch seller structured subscriptions"))

    sub_op = SubscriptionOperation(OperatorType.API)
    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links", new=fake_fetch
    ), patch(
        "app.services.oc_upstream_subscription.fetch_upstream_subscription_body",
        new=structured_fetch,
    ):
        async with GetDB() as db:
            u = (
                await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(selectinload(User.groups).selectinload(Group.inbounds))
                )
            ).scalar_one()
            validated = await sub_op.validated_user(u)
        web = await generate_subscription(validated, "links", False)
        xray = await generate_subscription(validated, "xray", False)
        sing = await generate_subscription(validated, "sing_box", False)
        clash_meta = await generate_subscription(validated, "clash_meta", False)

    web_lines = [ln.strip() for ln in web.splitlines() if ln.strip()]
    assert len(web_lines) == 8

    xray_data = json.loads(xray)
    assert len(xray_data) == 8
    expected_fps = _endpoint_identity_fingerprints(web_lines)
    assert len(expected_fps) == 8
    assert _xray_identity_fingerprints(xray_data) == expected_fps

    sing_doc = json.loads(sing)
    assert _singbox_identity_fingerprints(sing_doc) == expected_fps

    import yaml

    clash_doc = yaml.safe_load(clash_meta) or {}
    assert _clash_identity_fingerprints(clash_doc) == expected_fps
    structured_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_stored_subscription_link_not_used_when_config_not_synced():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        stored = _link("revoked-uid", "revoked.example", 443, "Revoked")
        panel, rows = await _panel(db, tenant, admin, [stored], name="PRev")
        row = rows[0]
        payload = row.source_payload if isinstance(row.source_payload, dict) else {}
        row.source_payload = {**payload, "subscription_link": stored}
        tag = destination_virtual_inbound_tag(panel.id, row.destination_config_id)
        inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one()
        group = Group(name="g", inbounds=[inbound], tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
                last_synced_configs=[],
                external_subscription_url="https://203.0.113.8/sub/rev",
            )
        )
        await db.commit()
        uid = user.id

    async def fail_fetch(_mapping):
        raise TimeoutError

    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links", new=fail_fetch
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(db, uid, [tag], {}, user_tenant_id=None)

    assert links == []


@pytest.mark.asyncio
async def test_discovery_link_never_substituted_when_panel_links_fetch_fails():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        stored = _link("stored-uid", "stored.example", 8443, "Stored")
        panel, rows = await _panel(db, tenant, admin, [stored], name="PStore")
        row = rows[0]
        payload = row.source_payload if isinstance(row.source_payload, dict) else {}
        payload = {**payload, "subscription_link": stored}
        row.source_payload = payload
        tag = destination_virtual_inbound_tag(panel.id, row.destination_config_id)
        inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one()
        group = Group(name="g", inbounds=[inbound], tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
                last_synced_configs=[row.destination_config_id],
                external_subscription_url="https://203.0.113.9/sub/store",
            )
        )
        await db.commit()
        uid = user.id

    async def fail_fetch(_mapping):
        raise TimeoutError("synthetic seller timeout")

    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links", new=fail_fetch
    ):
        async with GetDB() as db:
            state = await load_oc_subscription_links_state(db, uid, [tag], user_tenant_id=None)
            diag: dict = {}
            links = await build_oc_subscription_links_from_state(state, {}, diagnostics=diag)

    # ``subscription_link`` on the destination row is the discovery/test user's link.  When the
    # customer's own pool cannot be loaded nothing may be substituted: the discovery credential
    # belongs to another identity.  The failure is reported, not mistaken for an empty selection.
    assert links == []
    assert diag["degraded"] is True and diag["all_failed"] is True
    assert diag["failed_panels"] == [panel.id]


@pytest.mark.asyncio
async def test_subscription_userinfo_headers_unchanged_for_oc_user():
    from app.models.user import UsersResponseWithInbounds

    sub_op = SubscriptionOperation(OperatorType.API)
    from datetime import UTC, datetime

    user = UsersResponseWithInbounds(
        id=1,
        username="quota_user",
        status=UserStatus.active,
        used_traffic=12345,
        data_limit=999000,
        expire=None,
        inbounds=[],
        proxy_settings={},
        admin_id=1,
        created_at=datetime.now(UTC),
    )
    from app.models.settings import Subscription as SubSettings

    sub_settings = SubSettings(rules=[])
    headers = sub_op.create_response_headers(user, "https://example.test/sub/token", sub_settings)
    assert "subscription-userinfo" in headers
    assert "download=12345" in headers["subscription-userinfo"]
    assert "total=999000" in headers["subscription-userinfo"]


def _default_subscription_rules():
    """Mirror dashboard defaultSubscriptionRules (subscription-settings-schema.ts)."""
    from app.models.settings import ConfigFormat, SubRule

    return [
        SubRule(
            pattern=r"^([Cc]lash[\-\.]?[Vv]erge|[Cc]lash[\-\.]?[Mm]eta|[Ff][Ll][Cc]lash|[Mm]ihomo)",
            target=ConfigFormat.clash_meta,
        ),
        SubRule(pattern=r"^([Cc]lash|[Ss]tash)", target=ConfigFormat.clash),
        SubRule(
            pattern=r"^(SFA|SFI|SFM|SFT|[Kk]aring|[Hh]iddify[Nn]ext)|.*[Ss]ing[\-b]?ox.*",
            target=ConfigFormat.sing_box,
        ),
        SubRule(pattern=r"^(SS|SSR|SSD|SSS|Outline|Shadowsocks|SSconf)", target=ConfigFormat.outline),
        SubRule(pattern=r"^([Vv]2rayNG|[Vv]2rayN)", target=ConfigFormat.links),
        SubRule(pattern=r"^([Ss]treisand|[Hh]app|[Kk]tor\-client)", target=ConfigFormat.xray),
        SubRule(pattern=r".*", target=ConfigFormat.links_base64),
    ]


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        ("Happ/2.0", ConfigFormat.xray),
        ("v2rayN/7.12.8 (Windows NT 10.0)", ConfigFormat.links),
    ],
)
def test_subscription_ua_rules_match_happ_and_v2rayn(user_agent, expected):
    rules = _default_subscription_rules()
    matched = SubscriptionOperation.detect_client_rule(user_agent, rules)
    assert matched is not None
    assert matched.target == expected


@pytest.mark.asyncio
async def test_happ_xray_subscription_includes_all_selected_oc_links():
    """Happ consumes Xray JSON; OC users must not receive an empty [] body."""
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        vless_link = _link("vless-uuid-happ-a", "vless-a.example", 443, "VlessA")
        vless_link2 = _link("vless-uuid-happ-b", "vless-b.example", 444, "VlessB")
        panel, rows = await _panel(db, tenant, admin, [vless_link, vless_link2], name="PHapp")
        tag = destination_virtual_inbound_tag(panel.id, rows[0].destination_config_id)
        tag2 = destination_virtual_inbound_tag(panel.id, rows[1].destination_config_id)
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_([tag, tag2])))).scalars().all()
        group = Group(name="happ_grp", inbounds=list(inbounds), tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
                last_synced_configs=[rows[0].destination_config_id, rows[1].destination_config_id],
                external_subscription_url="https://203.0.113.50/sub/synthetic",
            )
        )
        await db.commit()
        uid = user.id

    cust = "\n".join([vless_link, vless_link2])
    structured_fetch = AsyncMock(side_effect=AssertionError("seller structured fetch must not run"))

    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links",
        new=AsyncMock(return_value=cust.split("\n")),
    ), patch(
        "app.services.oc_upstream_subscription.fetch_upstream_subscription_body",
        new=structured_fetch,
    ):
        async with GetDB() as db:
            u = (
                await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(selectinload(User.groups).selectinload(Group.inbounds))
                )
            ).scalar_one()
            validated = await SubscriptionOperation(OperatorType.API).validated_user(u)
        xray = await generate_subscription(validated, "xray", False)

    from tests.oc_format_identity import fp, xray_identities

    data = json.loads(xray)
    assert isinstance(data, list)
    assert len(data) == 2
    # Native Xray subscription schema: every entry is a full config with remarks + outbounds.
    for item in data:
        assert item.get("remarks")
        assert item["outbounds"][0]["protocol"] == "vless"
    assert set(xray_identities(data)) == {
        ("vless", "vless-a.example", 443, fp("vless-uuid-happ-a")),
        ("vless", "vless-b.example", 444, fp("vless-uuid-happ-b")),
    }
    structured_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_v2rayn_links_subscription_matches_web_identity_set():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        links = [
            _link("vn1", "a.example", 443, "A"),
            _link("vn2", "b.example", 444, "B"),
            _link("vn3", "c.example", 445, "C"),
        ]
        panel, rows = await _panel(db, tenant, admin, links, name="PV2")
        tags = [destination_virtual_inbound_tag(panel.id, r.destination_config_id) for r in rows]
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(tags)))).scalars().all()
        group = Group(name="v2n", inbounds=list(inbounds), tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
                external_subscription_url="https://203.0.113.51/sub/synthetic",
            )
        )
        await db.commit()
        uid = user.id

    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links",
        new=AsyncMock(return_value=links),
    ):
        async with GetDB() as db:
            u = (
                await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(selectinload(User.groups).selectinload(Group.inbounds))
                )
            ).scalar_one()
            validated = await SubscriptionOperation(OperatorType.API).validated_user(u)
        web = await generate_subscription(validated, "links", False)
        plain = await generate_subscription(validated, "links", False, user_agent="v2rayN/7.0.0")

    web_ids = _link_identities([ln.strip() for ln in web.splitlines() if ln.strip()])
    plain_ids = _link_identities([ln.strip() for ln in plain.splitlines() if ln.strip()])
    assert len(web_ids) == 3
    assert plain_ids == web_ids


@pytest.mark.asyncio
async def test_subscription_cache_isolated_by_output_format():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        link = _link("cache-u", "cache.example", 443, "One")
        panel, rows = await _panel(db, tenant, admin, [link], name="PCache")
        tag = destination_virtual_inbound_tag(panel.id, rows[0].destination_config_id)
        inbound = (
            await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))
        ).scalar_one()
        group = Group(name="cache_g", inbounds=[inbound], tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
                last_synced_configs=[rows[0].destination_config_id],
                external_subscription_url="https://203.0.113.52/sub/synthetic",
            )
        )
        await db.commit()
        uid = user.id

    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links",
        new=AsyncMock(return_value=[link]),
    ):
        async with GetDB() as db:
            u = (
                await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(selectinload(User.groups).selectinload(Group.inbounds))
                )
            ).scalar_one()
            validated = await SubscriptionOperation(OperatorType.API).validated_user(u)
        links_body = await generate_subscription(validated, "links", False)
        xray_body = await generate_subscription(validated, "xray", False)

    assert links_body.strip() != xray_body.strip()
    assert len([ln for ln in links_body.splitlines() if ln.strip()]) == 1
    assert len(json.loads(xray_body)) == 1


@pytest.mark.asyncio
async def test_six_vless_two_vmess_identity_parity_via_oc_link_builder():
    """6 VLESS + 2 VMess through collect/build → all formats share the same endpoint identities."""
    import base64

    import yaml

    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        vless_defs = [(f"vl-{i}", f"vhost{i}.example", 4400 + i, f"V{i}") for i in range(6)]
        vless_links = [_link(uid, host, port, remark) for uid, host, port, remark in vless_defs]
        vmess_defs = [
            ("vm-a-uuid-000000000001", "vmhost-a.example", 50768, "VmA"),
            ("vm-b-uuid-000000000002", "vmhost-b.example", 50765, "VmB"),
        ]
        vmess_links = []
        for vid, host, port, ps in vmess_defs:
            obj = {
                "v": "2",
                "ps": ps,
                "add": host,
                "port": port,
                "id": vid,
                "aid": 0,
                "net": "ws",
                "type": "none",
                "tls": "tls",
                "host": "cdn.example",
            }
            vmess_links.append("vmess://" + base64.b64encode(json.dumps(obj).encode()).decode())
        upstream_links = vless_links + vmess_links
        panel, rows = await _panel(db, tenant, admin, upstream_links, name="P682")
        tags = [destination_virtual_inbound_tag(panel.id, r.destination_config_id) for r in rows]
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(tags)))).scalars().all()
        group = Group(name="mix8", inbounds=list(inbounds), tenant_id=tenant.id, workspace_id=admin.workspace_id)
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
                external_subscription_url="https://203.0.113.82/sub/synthetic",
            )
        )
        await db.commit()
        uid = user.id

    structured_fetch = AsyncMock(side_effect=AssertionError("seller structured fetch must not run"))
    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links",
        new=AsyncMock(return_value=upstream_links),
    ), patch(
        "app.services.oc_upstream_subscription.fetch_upstream_subscription_body",
        new=structured_fetch,
    ):
        async with GetDB() as db:
            collected = await collect_oc_subscription_links_for_user(db, uid, tags, {"USERNAME": "cust"}, user_tenant_id=None)
            u = (
                await db.execute(
                    select(User)
                    .where(User.id == uid)
                    .options(selectinload(User.groups).selectinload(Group.inbounds))
                )
            ).scalar_one()
            validated = await SubscriptionOperation(OperatorType.API).validated_user(u)
        links_out = await generate_subscription(validated, "links", False)
        xray_out = await generate_subscription(validated, "xray", False)
        sing_out = await generate_subscription(validated, "sing_box", False)
        clash_out = await generate_subscription(validated, "clash_meta", False)

    assert len(collected) == 8
    expected = _endpoint_identity_fingerprints(collected)
    assert len(expected) == 8

    web_lines = [ln.strip() for ln in links_out.splitlines() if ln.strip()]
    assert len(web_lines) == 8
    assert _endpoint_identity_fingerprints(web_lines) == expected
    for line in web_lines:
        if line.lower().startswith("vmess://"):
            assert "#" not in line.split("?", 1)[0]

    xray_data = json.loads(xray_out)
    assert len(xray_data) == 8
    assert _xray_identity_fingerprints(xray_data) == expected

    sing_doc = json.loads(sing_out)
    assert len(_singbox_identity_fingerprints(sing_doc)) == 8
    assert _singbox_identity_fingerprints(sing_doc) == expected

    clash_doc = yaml.safe_load(clash_out) or {}
    assert len(_clash_identity_fingerprints(clash_doc)) == 8
    assert _clash_identity_fingerprints(clash_doc) == expected
    structured_fetch.assert_not_called()


def test_upstream_xray_filter_drops_unselected_seller_entries():
    """Regression: seller JSON must not leak configs outside the authorized link set."""
    allowed = [_link("only-me", "sel.example", 443, "Sel")]
    upstream = json.dumps(
        [
            {"protocol": "vless", "id": "only-me", "add": "sel.example", "port": 443, "ps": "Sel"},
            {"protocol": "vless", "id": "stranger", "add": "other.example", "port": 443, "ps": "Other"},
            {"protocol": "vless", "id": "only-me", "add": "wrong-port.example", "port": 8443, "ps": "Wrong"},
        ]
    )
    filtered = filter_upstream_subscription_body(upstream, "xray", allowed)
    data = json.loads(filtered)
    assert len(data) == 1
    assert data[0]["add"] == "sel.example"
