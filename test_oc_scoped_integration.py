"""PasarGuard <-> Outbound Center: scoped connection contract.

Runs the *real* PasarGuard code (router handlers, OC client, sync worker, usage
poller) against a fake Outbound Center HTTP server that enforces the new contract
(global ``X-Integration-Token`` + per-connection ``X-OC-Connection-Token``).

SAFETY: uses a private in-memory SQLite database. ``app.db.base.SessionLocal`` is
patched, the live database from ``.env`` is never contacted, and nothing is deleted
from any real table. The guard fixture refuses to run if the patch did not apply.
"""
from __future__ import annotations

import datetime
import json as jsonlib
from types import SimpleNamespace
from unittest.mock import patch

if not hasattr(datetime, "UTC"):  # py<3.11 compat used across this repo's tests
    datetime.UTC = datetime.timezone.utc

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select

from app.db.models import User, Workspace
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.base as db_base
from app.db.base import Base
import app.db.models  # noqa: F401  (register tables)
import app.db.models_oc  # noqa: F401
from app.db.models_oc import (
    OCIntegration,
    OCPanel,
    OCPanelConfig,
    OCPanelGroup,
    OCSyncState,
    OCUserMapping,
    TenantTelegramConnection,
)
from app.jobs.process_oc_sync import process_oc_sync
from app.node import oc_sync
from app.node.oc_usage import fetch_oc_user_usage, record_oc_user_usages
from app.routers import integration as integ
from app.services import oc_integration_client as client_mod
from app.services.oc_connection_credentials import encrypt_connection_token
from app.services.oc_integration_client import (
    OcIntegrationApiError,
    get_active_integration,
    http_exception_from_oc_error,
)
from app.services import oc_telegram_connection as tg
from app.utils.crypto import encrypt_secret
from config import runtime_settings

GLOBAL_TOKEN = "global-int-token"
CONN_TOKEN = "intent-1.sig-aaaa"
OC_ACCOUNT = 55


# ---------------------------------------------------------------------------
# Fake Outbound Center
# ---------------------------------------------------------------------------

class FakeOC:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.bot_url: str | None = "https://t.me/real_oc_bot?start=pgconnect_{intent}"
        self.verify_code = "12345"
        self.verify_error: tuple[int, str] | None = None
        self.issue_token: str | None = CONN_TOKEN
        self.valid_token = CONN_TOKEN
        self.revoked = False
        self.panel_status = "ACTIVE"
        self.users: dict[str, dict] = {}
        self.fail_next: list[tuple[int, str]] = []

    def record(self, request: web.Request, body=None):
        self.calls.append(
            {
                "method": request.method,
                "path": request.path,
                "global": request.headers.get("X-Integration-Token"),
                "conn": request.headers.get("X-OC-Connection-Token"),
                "body": body,
            }
        )

    def last(self, method: str, suffix: str) -> dict:
        for call in reversed(self.calls):
            if call["method"] == method and call["path"].endswith(suffix):
                return call
        raise AssertionError(f"no {method} *{suffix} call; got {[(c['method'], c['path']) for c in self.calls]}")

    @staticmethod
    def err(status: int, detail) -> web.Response:
        return web.json_response({"detail": detail}, status=status)

    def build(self) -> web.Application:
        app = web.Application()
        r = app.router

        @web.middleware
        async def guard(request: web.Request, handler):
            body = None
            if request.can_read_body:
                try:
                    body = await request.json()
                except Exception:
                    body = None
            self.record(request, body)
            request["body"] = body
            if request.headers.get("X-Integration-Token") != GLOBAL_TOKEN:
                return self.err(401, "Invalid integration token")
            if self.fail_next:
                status, detail = self.fail_next.pop(0)
                return self.err(status, detail)
            return await handler(request)

        app.middlewares.append(guard)

        def scoped(handler, *, allow_revoked=False):
            async def wrapper(request: web.Request):
                token = request.headers.get("X-OC-Connection-Token")
                if not token:
                    return self.err(401, "CONNECTION_TOKEN_REQUIRED")
                if token != self.valid_token:
                    return self.err(403, "CONNECTION_TOKEN_INVALID")
                if self.revoked and not allow_revoked:
                    return self.err(403, "CONNECTION_REVOKED")
                return await handler(request)

            return wrapper

        async def register(request):
            intent = request["body"]["intent_id"]
            payload = {"intent_id": intent, "status": "PENDING"}
            payload["bot_url"] = self.bot_url.format(intent=intent) if self.bot_url else None
            return web.json_response(payload)

        async def verify(request):
            if self.verify_error:
                return self.err(*self.verify_error)
            if request["body"].get("code") != self.verify_code:
                return self.err(403, "Invalid verification code")
            data = {"account_id": OC_ACCOUNT, "telegram_id": 999, "intent_id": request.match_info["iid"]}
            if self.issue_token:
                data["connection_token"] = self.issue_token
            return web.json_response(data)

        async def account_panels(request):
            default = [{"subscription_id": 11, "panel_type": "PASARGUARD", "name": "Panel 11 (agent)", "status": "ACTIVE"}]
            items = getattr(self, "account_panel_items", None) or default
            return web.json_response({"items": items})

        async def panel_get(request):
            return web.json_response({"id": int(request.match_info["pid"]), "status": self.panel_status})

        async def groups(request):
            if self.panel_status != "ACTIVE":
                return self.err(409, "PANEL_INACTIVE")
            return web.json_response({"groups": [{"id": "1", "name": "Premium"}, {"id": "2", "name": "Basic"}]})

        async def configs(request):
            if self.panel_status != "ACTIVE":
                return self.err(409, "PANEL_INACTIVE")
            return web.json_response(
                {
                    "configs": [
                        {
                            "id": "vless-tcp",
                            "name": "vless-tcp",
                            "protocol": "vless",
                            "network": "tcp",
                            "group_mapping": {"supported": True, "groups": ["1", "2"]},
                        },
                        {
                            "id": "trojan-ws",
                            "name": "trojan-ws",
                            "protocol": "trojan",
                            "network": "ws",
                            "group_mapping": {"supported": True, "groups": ["1"]},
                        },
                    ]
                }
            )

        async def test_user(request):
            if self.panel_status != "ACTIVE":
                return self.err(409, "PANEL_INACTIVE")
            return web.json_response({"external_user_id": "pasarguard_discovery_11", "status": "active"})

        async def put_user(request):
            if self.panel_status != "ACTIVE":
                return self.err(409, "PANEL_INACTIVE")
            ext = request.match_info["ext"]
            body = request["body"]
            self.users[ext] = body
            return web.json_response(
                {
                    "external_user_id": ext,
                    "status": "active",
                    "groups": body.get("groups", []),
                    "configs": body.get("configs", []),
                    "subscription_url": f"https://panel.example/sub/{ext}",
                }
            )

        async def delete_user(request):
            self.users.pop(request.match_info["ext"], None)
            return web.Response(status=204)

        async def usage(request):
            if self.panel_status != "ACTIVE":
                return self.err(409, "PANEL_INACTIVE")
            return web.json_response({"upload_bytes": 0, "download_bytes": 0, "total_bytes": 4096})

        async def revoke(request):
            self.revoked = True
            return web.Response(status=204)

        r.add_post("/v1/integration/pasarguard/connection-intents", register)
        r.add_post("/v1/integration/pasarguard/connection-intents/{iid}/verify", verify)
        r.add_post("/v1/integration/pasarguard/connection/revoke", scoped(revoke, allow_revoked=True))
        r.add_get("/v1/integration/accounts/{aid}/panels", scoped(account_panels))
        r.add_get("/v1/integration/panels/{pid}", scoped(panel_get))
        r.add_get("/v1/integration/panels/{pid}/groups", scoped(groups))
        r.add_get("/v1/integration/panels/{pid}/configs", scoped(configs))
        r.add_post("/v1/integration/panels/{pid}/test-user", scoped(test_user))
        r.add_put("/v1/integration/panels/{pid}/users/{ext}", scoped(put_user))
        r.add_delete("/v1/integration/panels/{pid}/users/{ext}", scoped(delete_user, allow_revoked=True))
        r.add_get("/v1/integration/panels/{pid}/users/{ext}/usage", scoped(usage))
        return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def env(monkeypatch):
    from app.services.oc_integration_client import call_oc_api as real_call_oc_api

    monkeypatch.setattr(integ, "call_oc_api", real_call_oc_api)
    monkeypatch.setattr(client_mod, "call_oc_api", real_call_oc_api)

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_base, "SessionLocal", factory)
    # SAFETY GUARD: every GetDB() must now hit the private in-memory engine.
    probe = db_base.GetDB()
    assert str(probe.db.bind.url).startswith("sqlite+aiosqlite://"), "refusing to run against a real DB"
    await probe.db.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fernet = Fernet(Fernet.generate_key())

    async def _fernet():
        return fernet

    monkeypatch.setattr("app.utils.crypto._get_fernet", _fernet)
    monkeypatch.setattr(runtime_settings, "oc_bot_username", "env_fallback_bot", raising=False)

    fake = FakeOC()
    runner = web.AppRunner(fake.build())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    from test_integration_admin import seed_tenant_admin_workspace, tenant_admin_details

    async with factory() as s:
        tenant_id, workspace_id, admin_id = await seed_tenant_admin_workspace(
            s, username="tenant-admin"
        )
        integration = OCIntegration(
            base_url=base_url,
            api_token_encrypted=await encrypt_secret(GLOBAL_TOKEN),
            token_preview="glob",
        )
        s.add(integration)
        sync_user = User(
            username=f"oc_sync_user_{admin_id}",
            admin_id=admin_id,
            workspace_id=workspace_id,
        )
        s.add(sync_user)
        await s.commit()
        integration_id = integration.id
        sync_user_id = sync_user.id

    admin_details = tenant_admin_details(
        admin_id=admin_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        username="tenant-admin",
    )
    yield SimpleNamespace(
        fake=fake,
        factory=factory,
        base_url=base_url,
        integration_id=integration_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        sync_user_id=sync_user_id,
        admin=admin_details,
        ctx=("tenant-admin", False, admin_details),
    )
    await runner.cleanup()
    await engine.dispose()


async def _add_connection(
    env,
    *,
    token: str | None = CONN_TOKEN,
    active=True,
    account=OC_ACCOUNT,
    tenant: int | None = None,
    binding_admin_id: int | None = None,
):
    tenant = tenant if tenant is not None else env.tenant_id
    binding_admin_id = binding_admin_id if binding_admin_id is not None else env.admin.id
    async with env.factory() as s:
        conn = TenantTelegramConnection(
            tenant_id=tenant,
            binding_admin_id=binding_admin_id,
            telegram_user_id=999,
            oc_account_id=account,
            status="active" if active else "revoked",
            active=active,
            oc_connection_token_encrypted=await encrypt_connection_token(token) if token else None,
        )
        s.add(conn)
        await s.commit()
        return conn.id


async def _add_panel(env, *, tenant: int | None = None, account=OC_ACCOUNT, source="11", sync_status="connected"):
    tenant = tenant if tenant is not None else env.tenant_id
    async with env.factory() as s:
        panel = OCPanel(
            integration_id=env.integration_id,
            source_panel_id=source,
            purchaser_identity=str(account),
            name="P",
            tenant_id=tenant,
            workspace_id=getattr(env, "workspace_id", None),
            oc_account_id=account,
            sync_status=sync_status,
        )
        s.add(panel)
        await s.commit()
        return panel.id


async def _add_mapping(env, panel_id, *, user_id=None, ext="ext-1", status="pending", configs=None):
    user_id = user_id if user_id is not None else env.sync_user_id
    async with env.factory() as s:
        m = OCUserMapping(user_id=user_id, panel_id=panel_id, external_user_id=ext, status=status)
        m.last_synced_configs = configs
        s.add(m)
        await s.commit()
        return m.id


async def _job(env, **kw) -> OCSyncState:
    async with env.factory() as s:
        return (await s.execute(select(OCSyncState).filter_by(**kw).order_by(OCSyncState.id.desc()))).scalars().first()


# ---------------------------------------------------------------------------
# Connection intent / bot_url / token storage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_prefers_bot_url_returned_by_oc(env):
    async with env.factory() as db:
        res = await integ.telegram_connection_start(db=db, user_context=env.ctx)
        pending = (await db.execute(select(TenantTelegramConnection))).scalars().one()
    assert res.bot_url == f"https://t.me/real_oc_bot?start=pgconnect_{res.intent_id}"
    assert pending.pending_bot_url == res.bot_url
    # the global token (not a connection token) is used for registering
    reg = env.fake.last("POST", "/connection-intents")
    assert reg["global"] == GLOBAL_TOKEN and reg["conn"] is None
    # the status endpoint serves the stored link
    async with env.factory() as db:
        st = await integ.telegram_connection_status(db=db, user_context=env.ctx)
    assert st.bot_url == res.bot_url and st.step == "waiting_for_code"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "oc_value",
    [None, "", "https://evil.example/phish?start=pgconnect_x", "http://t.me/real?start=pgconnect_x", "javascript:alert(1)"],
)
async def test_start_falls_back_to_env_username_for_missing_or_untrusted_oc_link(env, oc_value):
    env.fake.bot_url = oc_value
    async with env.factory() as db:
        res = await integ.telegram_connection_start(db=db, user_context=env.ctx)
    assert res.bot_url == f"https://t.me/env_fallback_bot?start=pgconnect_{res.intent_id}"


@pytest.mark.asyncio
async def test_start_without_oc_link_and_without_env_username_is_503_not_hardcoded(env, monkeypatch):
    monkeypatch.setattr(runtime_settings, "oc_bot_username", "", raising=False)
    env.fake.bot_url = None
    async with env.factory() as db:
        with pytest.raises(HTTPException) as exc:
            await integ.telegram_connection_start(db=db, user_context=env.ctx)
    assert exc.value.status_code == 503
    # and no bot name is baked into the source
    import inspect

    src = inspect.getsource(tg)
    assert "outboundino_bot" not in src.lower() and "outboundcenter_bot" not in src.lower()


@pytest.mark.asyncio
async def test_confirm_stores_encrypted_connection_token_and_activates(env):
    async with env.factory() as db:
        res = await integ.telegram_connection_start(db=db, user_context=env.ctx)
    async with env.factory() as db:
        out = await integ.telegram_connection_confirm(
            body=integ.TelegramConnectionConfirmRequest(code="12345"), db=db, user_context=env.ctx
        )
    assert out.active and out.oc_account_id == OC_ACCOUNT
    verify = env.fake.last("POST", f"/{res.intent_id}/verify")
    assert verify["global"] == GLOBAL_TOKEN
    async with env.factory() as db:
        row = (await db.execute(select(TenantTelegramConnection))).scalars().one()
    assert row.oc_connection_token_encrypted and CONN_TOKEN not in row.oc_connection_token_encrypted
    assert row.pending_intent_id is None and row.pending_bot_url is None
    from app.utils.crypto import decrypt_secret

    assert await decrypt_secret(row.oc_connection_token_encrypted) == CONN_TOKEN


@pytest.mark.asyncio
async def test_confirm_without_oc_token_does_not_activate(env):
    env.fake.issue_token = None  # old OC that does not issue connection credentials
    async with env.factory() as db:
        await integ.telegram_connection_start(db=db, user_context=env.ctx)
    async with env.factory() as db:
        with pytest.raises(HTTPException) as exc:
            await integ.telegram_connection_confirm(
                body=integ.TelegramConnectionConfirmRequest(code="12345"), db=db, user_context=env.ctx
            )
    assert exc.value.status_code == 502
    async with env.factory() as db:
        row = (await db.execute(select(TenantTelegramConnection))).scalars().one()
    assert row.status == "pending" and not row.active


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "oc_error, expected",
    [((403, "Invalid verification code"), 400), ((429, "Too many attempts"), 429), ((410, "Connection request expired"), 409)],
)
async def test_confirm_maps_oc_errors(env, oc_error, expected):
    async with env.factory() as db:
        await integ.telegram_connection_start(db=db, user_context=env.ctx)
    env.fake.verify_error = oc_error
    async with env.factory() as db:
        with pytest.raises(HTTPException) as exc:
            await integ.telegram_connection_confirm(
                body=integ.TelegramConnectionConfirmRequest(code="99999"), db=db, user_context=env.ctx
            )
    assert exc.value.status_code == expected


# ---------------------------------------------------------------------------
# Deterministic integration selection (no row is touched)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_active_integration_is_deterministic_and_deletes_nothing(env):
    async with env.factory() as s:
        for n in range(3):
            s.add(OCIntegration(base_url=f"http://other{n}", api_token_encrypted="x", token_preview="o", is_active=True))
        s.add(OCIntegration(base_url="http://off", api_token_encrypted="x", token_preview="o", is_active=False))
        await s.commit()
    picked = set()
    for _ in range(5):
        async with env.factory() as s:
            picked.add((await get_active_integration(s)).id)
    assert picked == {env.integration_id}  # lowest active id, every time
    async with env.factory() as s:
        assert len((await s.execute(select(OCIntegration))).scalars().all()) == 5  # nothing removed
    async with env.factory() as s:
        for row in (await s.execute(select(OCIntegration))).scalars().all():
            row.is_active = False
        await s.commit()
    async with env.factory() as s:
        with pytest.raises(HTTPException) as exc:
            await get_active_integration(s)
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# Scoped panel calls from the router
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_available_panels_sends_connection_token(env):
    await _add_connection(env)
    async with env.factory() as db:
        out = await integ.get_available_panels(db=db, user_context=env.ctx)
    assert [p.id for p in out.items] == [11]
    call = env.fake.last("GET", f"/accounts/{OC_ACCOUNT}/panels")
    assert call["global"] == GLOBAL_TOKEN and call["conn"] == CONN_TOKEN


@pytest.mark.asyncio
async def test_legacy_connection_without_token_asks_to_reconnect(env):
    await _add_connection(env, token=None)
    async with env.factory() as db:
        with pytest.raises(HTTPException) as exc:
            await integ.get_available_panels(db=db, user_context=env.ctx)
    assert exc.value.status_code == 409 and "Reconnect" in exc.value.detail
    assert not [c for c in env.fake.calls if "/accounts/" in c["path"]]  # nothing sent unscoped


@pytest.mark.asyncio
async def test_panel_calls_carry_connection_token_and_use_oc_group_list(env):
    await _add_connection(env)
    pid = await _add_panel(env)
    async with env.factory() as db:
        groups = await integ.get_groups(panel_id=pid, db=db, user_context=env.ctx)
        tu = await integ.create_test_user(panel_id=pid, db=db, user_context=env.ctx)
    assert [g["id"] if isinstance(g, dict) else g.id for g in groups.groups] == ["1", "2"]
    assert tu.test_user_id == "pasarguard_discovery_11"
    for suffix in ("/groups", "/test-user"):
        call = [c for c in env.fake.calls if c["path"].endswith(suffix)][-1]
        assert call["conn"] == CONN_TOKEN and call["global"] == GLOBAL_TOKEN


@pytest.mark.asyncio
async def test_wizard_sync_ignores_client_group_names_and_rejects_unknown_groups(env):
    await _add_connection(env)
    pid = await _add_panel(env)
    async with env.factory() as db:
        with pytest.raises(HTTPException) as exc:
            await integ.sync_configs_and_hosts(
                panel_id=pid,
                req=integ.SyncRequest(selected_group_ids=["999"], group_names={"999": "evil"}),
                db=db,
                user_context=env.ctx,
            )
    assert exc.value.status_code == 400
    async with env.factory() as db:
        out = await integ.sync_configs_and_hosts(
            panel_id=pid,
            req=integ.SyncRequest(selected_group_ids=["1"], group_names={"1": "client-chosen-name"}),
            db=db,
            user_context=env.ctx,
        )
    assert out.configs_created == 2
    async with env.factory() as s:
        groups = {g.source_group_id: g for g in (await s.execute(select(OCPanelGroup))).scalars().all()}
    assert groups["1"].source_name == "Premium" and groups["1"].is_selected  # OC's name wins
    assert groups["2"].source_name == "Basic" and not groups["2"].is_selected


@pytest.mark.asyncio
async def test_inactive_panel_error_is_mapped_and_persisted(env):
    await _add_connection(env)
    pid = await _add_panel(env)
    env.fake.panel_status = "SUSPENDED"
    async with env.factory() as db:
        with pytest.raises(HTTPException) as exc:
            await integ.get_groups(panel_id=pid, db=db, user_context=env.ctx)
    assert exc.value.status_code == 409
    async with env.factory() as s:
        assert (await s.get(OCPanel, pid)).sync_status == "inactive"


# ---------------------------------------------------------------------------
# Client error mapping for the new OC codes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "detail, status, expected",
    [
        ("CONNECTION_REVOKED", 403, 409),
        ("CONNECTION_TOKEN_INVALID", 403, 409),
        ("CONNECTION_TOKEN_REQUIRED", 401, 409),
        ("PANEL_INACTIVE", 409, 409),
        ("PANEL_NOT_FOUND", 404, 404),
        ("PANEL_UNAVAILABLE", 502, 503),
        ("PANEL_TYPE_UNSUPPORTED", 501, 502),
        ("{'code': 'GROUP_NOT_ALLOWED', 'message': 'x'}", 400, 400),
        ("Invalid integration token", 401, 503),
    ],
)
def test_oc_error_codes_map_to_actionable_pg_errors(detail, status, expected):
    assert http_exception_from_oc_error(OcIntegrationApiError(oc_status=status, detail=detail)).status_code == expected


# ---------------------------------------------------------------------------
# Sync worker: groups, scoping, subscription url, failure handling
# ---------------------------------------------------------------------------

async def _group_with_config(env, panel_id, *, selected=True):
    async with env.factory() as s:
        g = OCPanelGroup(panel_id=panel_id, source_group_id="2", source_name="Basic", is_selected=selected)
        s.add(g)
        await s.flush()
        s.add(
            OCPanelConfig(
                panel_id=panel_id, source_config_id="vless-tcp", source_name="vless-tcp", panel_group_id=g.id
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_desired_groups_are_real_selected_oc_groups(env):
    pid = await _add_panel(env)
    await _group_with_config(env, pid, selected=True)
    async with env.factory() as s:
        assert await oc_sync.desired_oc_group_ids(s, pid, ["vless-tcp"]) == ["2"]
        assert await oc_sync.desired_oc_group_ids(s, pid, ["unknown"]) == []
        assert await oc_sync.desired_oc_group_ids(s, pid, []) == []
    async with env.factory() as s:
        g = (await s.execute(select(OCPanelGroup))).scalars().one()
        g.is_selected = False
        await s.commit()
    async with env.factory() as s:
        assert await oc_sync.desired_oc_group_ids(s, pid, ["vless-tcp"]) == []


async def _queue_put(env, pid, payload):
    uid = env.sync_user_id
    async with env.factory() as s:
        s.add(
            OCSyncState(
                entity_type="user_mapping",
                entity_id=f"{uid}_{pid}",
                operation="create",
                idempotency_key=f"k-{pid}-{len(str(payload))}",
                payload=payload,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_worker_put_sends_groups_with_connection_token_and_stores_subscription_url(env):
    await _add_connection(env)
    pid = await _add_panel(env)
    mid = await _add_mapping(env, pid)
    await _queue_put(env, pid, {"groups": ["2"], "configs": ["vless-tcp"]})
    await process_oc_sync()

    put = env.fake.last("PUT", "/users/ext-1")
    # Worker sends groups first (configs empty), then configs on a follow-up PUT when needed.
    assert put["body"]["groups"] == ["2"]
    assert put["conn"] == CONN_TOKEN and put["global"] == GLOBAL_TOKEN
    async with env.factory() as s:
        m = await s.get(OCUserMapping, mid)
        assert m.status == "active" and m.last_synced_configs == ["vless-tcp"]
        assert m.external_subscription_url == "https://panel.example/sub/ext-1"
    assert (await _job(env, entity_id=f"{env.sync_user_id}_{pid}")).status == "completed"


@pytest.mark.asyncio
async def test_worker_inactive_panel_fails_fast_and_marks_panel(env):
    await _add_connection(env)
    pid = await _add_panel(env)
    mid = await _add_mapping(env, pid)
    env.fake.panel_status = "SUSPENDED"
    await _queue_put(env, pid, {"groups": [], "configs": ["vless-tcp"]})
    await process_oc_sync()
    job = await _job(env, entity_id=f"{env.sync_user_id}_{pid}")
    assert job.status == "failed" and job.attempts == 1  # permanent: no 3x retry loop
    assert "PANEL_INACTIVE" in (job.last_error or "")
    async with env.factory() as s:
        assert (await s.get(OCPanel, pid)).sync_status == "inactive"
        assert (await s.get(OCUserMapping, mid)).status == "pending"  # never claimed as synced
        assert await oc_sync.tenant_panel_allows_oc_user_mutations(s, pid) is False  # stops re-queueing


@pytest.mark.asyncio
async def test_worker_transient_error_is_retried_then_fails(env):
    await _add_connection(env)
    pid = await _add_panel(env)
    await _add_mapping(env, pid)
    await _queue_put(env, pid, {"groups": [], "configs": ["vless-tcp"]})
    env.fake.fail_next = [(502, "PANEL_UNAVAILABLE")] * 3
    # PANEL_UNAVAILABLE is transient: still pending after the 1st and 2nd attempt
    await process_oc_sync()
    eid = f"{env.sync_user_id}_{pid}"
    assert (await _job(env, entity_id=eid)).status == "pending"
    await process_oc_sync()
    assert (await _job(env, entity_id=eid)).status == "pending"
    await process_oc_sync()
    assert (await _job(env, entity_id=eid)).status == "failed"


@pytest.mark.asyncio
async def test_worker_connection_without_token_fails_permanently(env):
    await _add_connection(env, token=None)
    pid = await _add_panel(env)
    await _add_mapping(env, pid)
    await _queue_put(env, pid, {"groups": [], "configs": ["vless-tcp"]})
    await process_oc_sync()
    job = await _job(env, entity_id=f"{env.sync_user_id}_{pid}")
    assert job.status == "failed" and "Reconnect" in job.last_error
    assert not [c for c in env.fake.calls if c["method"] == "PUT"]  # nothing sent unscoped


# ---------------------------------------------------------------------------
# Revoke / delete propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_revoke_queues_deletes_then_oc_revoke_and_deletes_still_work(env):
    conn_id = await _add_connection(env)
    pid = await _add_panel(env)
    await _add_mapping(env, pid, ext="ext-del", status="active", configs=["vless-tcp"])

    async with env.factory() as s:
        await tg.revoke_connection(s, env.tenant_id, binding_admin_id=env.admin.id)
        await s.commit()

    async with env.factory() as s:
        jobs = (await s.execute(select(OCSyncState).order_by(OCSyncState.id))).scalars().all()
    assert [(j.entity_type, j.operation) for j in jobs] == [("user_mapping", "delete"), ("oc_connection", "revoke")]
    assert jobs[0].payload["connection_id"] == conn_id and jobs[0].payload["tenant_id"] == env.tenant_id

    await process_oc_sync()

    assert env.fake.revoked is True
    revoke_call = env.fake.last("POST", "/connection/revoke")
    assert revoke_call["conn"] == CONN_TOKEN and revoke_call["global"] == GLOBAL_TOKEN
    delete_call = env.fake.last("DELETE", "/users/ext-del")
    assert delete_call["conn"] == CONN_TOKEN  # authorised with the (now revoked) connection
    async with env.factory() as s:
        statuses = {j.operation: j.status for j in (await s.execute(select(OCSyncState))).scalars().all()}
        assert statuses == {"delete": "completed", "revoke": "completed"}
        m = (await s.execute(select(OCUserMapping))).scalars().one()
        assert m.status == "deleted" and m.external_subscription_url is None
    # after the revoke, scoped reads are refused by OC but we no longer make them
    async with env.factory() as db:
        assert await tg.get_active_connection(db, env.tenant_id, binding_admin_id=env.admin.id) is None


@pytest.mark.asyncio
async def test_revoke_propagation_is_retried_when_oc_is_down(env):
    await _add_connection(env)
    async with env.factory() as s:
        await tg.revoke_connection(s, env.tenant_id, binding_admin_id=env.admin.id)
        await s.commit()
    env.fake.fail_next = [(503, "down")]
    await process_oc_sync()
    job = await _job(env, entity_type="oc_connection")
    assert job.status == "pending" and job.attempts == 1 and env.fake.revoked is False
    await process_oc_sync()
    assert (await _job(env, entity_type="oc_connection")).status == "completed" and env.fake.revoked is True


@pytest.mark.asyncio
async def test_revoking_legacy_connection_without_token_queues_no_oc_revoke(env):
    await _add_connection(env, token=None)
    async with env.factory() as s:
        await tg.revoke_connection(s, env.tenant_id, binding_admin_id=env.admin.id)
        await s.commit()
    assert await _job(env, entity_type="oc_connection") is None


@pytest.mark.asyncio
async def test_delete_job_survives_panel_removal_using_snapshot(env):
    await _add_connection(env)
    pid = await _add_panel(env)
    await _add_mapping(env, pid, ext="ext-gone", status="active", configs=["vless-tcp"])
    async with env.factory() as s:
        panel = await s.get(OCPanel, pid)
        await oc_sync.remove_imported_panel(s, panel)
        await s.commit()
    await process_oc_sync()
    call = env.fake.last("DELETE", "/users/ext-gone")
    assert call["conn"] == CONN_TOKEN
    assert (await _job(env, operation="delete")).status == "completed"


@pytest.mark.asyncio
async def test_delete_does_not_use_another_accounts_connection(env):
    await _add_connection(env, token="other.sig", account=999)  # same tenant, different OC account
    pid = await _add_panel(env, account=OC_ACCOUNT)
    await _add_mapping(env, pid, ext="ext-x", status="active", configs=["vless-tcp"])
    async with env.factory() as s:
        await oc_sync.enqueue_mapping_delete_sync(
            s, 1, pid, (await s.execute(select(OCUserMapping))).scalars().one()
        )
        await s.commit()
    await process_oc_sync()
    assert not [c for c in env.fake.calls if c["method"] == "DELETE"]
    assert (await _job(env, operation="delete")).status == "failed"


# ---------------------------------------------------------------------------
# Panel metadata/config sync + usage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_panel_sync_stores_config_contract_and_status(env):
    await _add_connection(env)
    pid = await _add_panel(env, sync_status="pending")
    async with env.factory() as s:
        await oc_sync.sync_panel_from_outbound_center(s, pid)
    async with env.factory() as s:
        panel = await s.get(OCPanel, pid)
        cfgs = {c.source_config_id: c for c in (await s.execute(select(OCPanelConfig))).scalars().all()}
        groups = {g.source_group_id: g.source_name for g in (await s.execute(select(OCPanelGroup))).scalars().all()}
    assert panel.sync_status == "connected" and panel.last_sync_at is not None
    assert groups == {"1": "Premium", "2": "Basic"}
    assert (cfgs["trojan-ws"].protocol, cfgs["trojan-ws"].network) == ("trojan", "ws")
    assert cfgs["vless-tcp"].source_payload["group_mapping"]["groups"] == ["1", "2"]
    for call in env.fake.calls:
        if "/panels/" in call["path"]:
            assert call["conn"] == CONN_TOKEN


@pytest.mark.asyncio
async def test_panel_sync_inactive_updates_status_and_keeps_data(env):
    await _add_connection(env)
    pid = await _add_panel(env, sync_status="pending")
    async with env.factory() as s:
        await oc_sync.sync_panel_from_outbound_center(s, pid)
    env.fake.panel_status = "OFFLINE_BY_SYSTEM"
    async with env.factory() as s:
        await oc_sync.sync_panel_from_outbound_center(s, pid)
    async with env.factory() as s:
        assert (await s.get(OCPanel, pid)).sync_status == "inactive"
        assert len((await s.execute(select(OCPanelConfig))).scalars().all()) == 2  # not wiped
        assert not any(c.source_missing for c in (await s.execute(select(OCPanelConfig))).scalars().all())


@pytest.mark.asyncio
async def test_panel_sync_after_oc_revoke_marks_panel(env):
    await _add_connection(env)
    pid = await _add_panel(env)
    env.fake.revoked = True
    async with env.factory() as s:
        with pytest.raises(OcIntegrationApiError):
            await oc_sync.sync_panel_from_outbound_center(s, pid)
    async with env.factory() as s:
        assert (await s.get(OCPanel, pid)).sync_status == "connection_revoked"


@pytest.mark.asyncio
async def test_usage_fetch_and_poll_use_connection_token(env):
    await _add_connection(env)
    pid = await _add_panel(env)
    mid = await _add_mapping(env, pid, ext="ext-u", status="active", configs=["vless-tcp"])
    async with aiohttp.ClientSession() as session:
        direct = await fetch_oc_user_usage(
            env.base_url, GLOBAL_TOKEN, "11", "ext-u", session=session, connection_token=CONN_TOKEN
        )
        assert direct == 4096
        # without a connection token OC refuses -> None, never a fabricated value
        assert await fetch_oc_user_usage(env.base_url, GLOBAL_TOKEN, "11", "ext-u", session=session) is None

    seen = []

    async def fake_account(mapping_id, cumulative):
        seen.append((mapping_id, cumulative))
        return 0

    with patch("app.node.oc_usage.account_mapping_usage", new=fake_account):
        await record_oc_user_usages()
    assert seen == [(mid, 4096)]
    assert env.fake.last("GET", "/users/ext-u/usage")["conn"] == CONN_TOKEN


@pytest.mark.asyncio
async def test_usage_poll_skips_mappings_without_usable_connection(env):
    await _add_connection(env, token=None)
    pid = await _add_panel(env)
    await _add_mapping(env, pid, ext="ext-u", status="active", configs=["vless-tcp"])
    seen = []

    async def fake_account(mapping_id, cumulative):
        seen.append(mapping_id)
        return 0

    with patch("app.node.oc_usage.account_mapping_usage", new=fake_account):
        await record_oc_user_usages()
    assert seen == []
    assert not [c for c in env.fake.calls if c["path"].endswith("/usage")]
