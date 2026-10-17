"""
Task 4C — multi-panel subscription aggregation regression tests.
"""
import asyncio
import datetime
import uuid

if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc

from sqlalchemy import delete, select

from app.db import GetDB
from app.db.models import Admin, Group, ProxyHost, ProxyInbound, Tenant, TenantStatus, User, UserStatus
from app.db.models_oc import (
    OCIntegration,
    OCPanel,
    OCPanelConfig,
    OCUserMapping,
    TenantTelegramConnection,
    TenantTelegramConnectionStatus,
)
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.subscription.config_cache import _cache as sub_cache
from app.subscription.share import generate_subscription


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


async def _oc_panel_with_host(db, integration_id: int, panel_key: str, tenant_id: int | None, address: str, remark: str):
    panel = OCPanel(
        integration_id=integration_id,
        source_panel_id=f"sp_{panel_key}",
        purchaser_identity="t4c",
        name=f"Panel {panel_key}",
        tenant_id=tenant_id,
    )
    db.add(panel)
    await db.flush()
    tag = f"oc_{panel.id}_{panel_key}"
    inbound = ProxyInbound(tag=tag)
    db.add(inbound)
    await db.flush()
    host = ProxyHost(
        remark=remark,
        priority=1,
        address={address},
        port=443,
        path=None,
        allowinsecure=None,
        alpn=[],
        status=[],
        is_disabled=False,
    )
    host.inbound = inbound
    db.add(host)
    await db.flush()
    cfg = OCPanelConfig(
        panel_id=panel.id,
        source_config_id=panel_key,
        source_name=f"Cfg {panel_key}",
        virtual_inbound_tag=tag,
        source_missing=False,
        protocol="vless",
        network="tcp",
        port=443,
    )
    db.add(cfg)
    await db.flush()
    return panel, inbound, host, cfg


def _mapping(user_id: int, panel_id: int, configs: list[str], status: str = "active"):
    return OCUserMapping(
        user_id=user_id,
        panel_id=panel_id,
        external_user_id=str(uuid.uuid4()),
        status=status,
        last_synced_configs=(configs if status == "active" and configs else None),
    )


async def _sub_for(user_id: int) -> str:
    sub_cache.clear()
    sub_op = SubscriptionOperation(operator_type=OperatorType.API)
    async with GetDB() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        validated = await sub_op.validated_user(user)
        return await generate_subscription(validated, "links", False)


async def run_tests():
    print("=== Task 4C Subscription Aggregation Tests ===\n")
    sub_op = SubscriptionOperation(operator_type=OperatorType.API)
    cleanup = []
    group_id = None
    native_inbound_id = None

    admin_hashed = None
    async with GetDB() as db:
        admin = (await db.execute(select(Admin).limit(1))).scalar_one()
        admin_hashed = admin.hashed_password

    print("1–2. Single and multiple active panels")
    async with GetDB() as db:
        intg = OCIntegration(base_url=f"http://{_uid('t4c')}", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        pa, in_a, _, _ = await _oc_panel_with_host(db, intg.id, "pa", None, "198.51.100.11", "Remark PA")
        pb, in_b, _, _ = await _oc_panel_with_host(db, intg.id, "pb", None, "198.51.100.12", "Remark PB")
        pc, in_c, _, _ = await _oc_panel_with_host(db, intg.id, "pc", None, "198.51.100.13", "Remark PC")
        native = ProxyInbound(tag=f"native_{uuid.uuid4().hex[:6]}")
        db.add(native)
        await db.flush()
        group = Group(name=_uid("g4c"), inbounds=[native, in_a, in_b, in_c])
        db.add(group)
        await db.flush()
        user = User(
            username=_uid("u4c"),
            status=UserStatus.active,
            data_limit=0,
            admin_id=admin.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            sub_token=_uid("sub"),
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        uid = user.id
        token = user.sub_token
        group_id = group.id
        native_inbound_id = native.id
        cleanup.extend([intg, pa, pb, pc, native, group, user, in_a, in_b, in_c])

        # Single panel A
        db.add(_mapping(uid, pa.id, ["pa"]))
        await db.commit()

    out_a = await _sub_for(uid)
    assert "198.51.100.11" in out_a, f"expected panel A address in subscription, got: {out_a[:200]!r}"
    assert "198.51.100.12" not in out_a
    print("   [x] Single active panel A")

    async with GetDB() as db:
        db.add_all([_mapping(uid, pb.id, ["pb"]), _mapping(uid, pc.id, ["pc"])])
        await db.commit()
    out_abc = await _sub_for(uid)
    assert "198.51.100.11" in out_abc and "198.51.100.12" in out_abc and "198.51.100.13" in out_abc
    print("   [x] Multiple active panels A+B+C")

    print("\n3–5. Pending / failed / deleted mappings")
    async with GetDB() as db:
        user = await _get_user(uid)
        await db.execute(delete(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == pa.id))
        db.add(_mapping(uid, pa.id, [], status="pending"))
        await db.commit()
    out_pending = await _sub_for(uid)
    assert "198.51.100.11" not in out_pending and "198.51.100.12" in out_pending
    print("   [x] Pending panel excluded, active B remains")

    async with GetDB() as db:
        await db.execute(delete(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == pa.id))
        failed = OCUserMapping(
            user_id=uid,
            panel_id=pa.id,
            external_user_id=str(uuid.uuid4()),
            status="active",
            last_synced_configs=None,
        )
        db.add(failed)
        await db.commit()
    out_failed = await _sub_for(uid)
    assert "198.51.100.11" not in out_failed
    print("   [x] Failed/unprovisioned mapping excluded")

    async with GetDB() as db:
        row = (
            await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == pb.id)
            )
        ).scalar_one()
        row.status = "deleted"
        row.last_synced_configs = []
        await db.commit()
    out_deleted = await _sub_for(uid)
    assert "198.51.100.12" not in out_deleted and "198.51.100.13" in out_deleted
    print("   [x] Deleted mapping excluded immediately")

    print("\n6–7. All OC inactive / partial failure")
    async with GetDB() as db:
        await db.execute(delete(OCUserMapping).where(OCUserMapping.user_id == uid))
        await db.commit()
    out_none = await _sub_for(uid)
    assert "198.51.100.11" not in out_none and "198.51.100.13" not in out_none
    print("   [x] No runtime OC resources when all mappings inactive")

    async with GetDB() as db:
        db.add(_mapping(uid, pa.id, ["pa"]))
        db.add(_mapping(uid, pc.id, ["pc"]))
        await db.commit()
    out_partial = await _sub_for(uid)
    assert "198.51.100.11" in out_partial and "198.51.100.13" in out_partial
    print("   [x] Partial failure: healthy panels still present")

    print("\n8. Group transition keeps sub_token stable")
    async with GetDB() as db:
        intg2 = OCIntegration(base_url=f"http://{_uid('t4c2')}", api_token_encrypted="e", token_preview="t")
        db.add(intg2)
        await db.flush()
        p_new, in_new, _, _ = await _oc_panel_with_host(db, intg2.id, "pnew", None, "198.51.100.99", "Remark NEW")
        native = (await db.execute(select(ProxyInbound).where(ProxyInbound.id == native_inbound_id))).scalar_one()
        group = (await db.execute(select(Group).where(Group.id == group_id))).scalar_one()
        from app.db.crud.group import load_group_attrs

        await load_group_attrs(group, load_users=False, load_inbounds=True)
        group.inbounds = [native, in_new]
        await db.flush()
        await db.execute(delete(OCUserMapping).where(OCUserMapping.user_id == uid))
        db.add(_mapping(uid, p_new.id, ["pnew"]))
        await db.commit()
        cleanup.extend([intg2, p_new, in_new])
    user_after = await _get_user(uid)
    assert user_after.sub_token == token
    out_new = await _sub_for(uid)
    assert "198.51.100.99" in out_new and "198.51.100.13" not in out_new
    print("   [x] Panel transition + stable sub_token")

    print("\n9. Tenant isolation")
    async with GetDB() as db:
        t_a = Tenant(name=_uid("ta"), status=TenantStatus.active)
        t_b = Tenant(name=_uid("tb"), status=TenantStatus.active)
        db.add_all([t_a, t_b])
        await db.flush()
        role_id = admin.role_id
        adm_a = Admin(username=_uid("adma"), hashed_password=admin_hashed, tenant_id=t_a.id, role_id=role_id)
        adm_b = Admin(username=_uid("admb"), hashed_password=admin_hashed, tenant_id=t_b.id, role_id=role_id)
        db.add_all([adm_a, adm_b])
        await db.flush()
        intg_t = OCIntegration(base_url=f"http://{_uid('t4ct')}", api_token_encrypted="e", token_preview="t")
        db.add(intg_t)
        await db.flush()
        p_ta, in_ta, _, _ = await _oc_panel_with_host(db, intg_t.id, "ta", t_a.id, "198.51.100.21", "Tenant A")
        p_tb, in_tb, _, _ = await _oc_panel_with_host(db, intg_t.id, "tb", t_b.id, "198.51.100.22", "Tenant B")
        for tenant in (t_a, t_b):
            db.add(
                TenantTelegramConnection(
                    tenant_id=tenant.id,
                    telegram_user_id=12345,
                    oc_account_id=1,
                    status=TenantTelegramConnectionStatus.active.value,
                    active=True,
                )
            )
        await db.flush()
        grp_t = Group(name=_uid("gt"), inbounds=[in_ta, in_tb])
        db.add(grp_t)
        await db.flush()
        u_ta = User(
            username=_uid("uta"),
            status=UserStatus.active,
            data_limit=0,
            admin_id=adm_a.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        u_ta.groups = [grp_t]
        db.add(u_ta)
        await db.flush()
        db.add_all([_mapping(u_ta.id, p_ta.id, ["ta"]), _mapping(u_ta.id, p_tb.id, ["tb"])])
        await db.commit()
        u_ta_id = u_ta.id
        cleanup.extend([t_a, t_b, adm_a, adm_b, intg_t, p_ta, p_tb, grp_t, u_ta, in_ta, in_tb])

    out_ta = await _sub_for(u_ta_id)
    assert "198.51.100.21" in out_ta and "198.51.100.22" not in out_ta
    print("   [x] No cross-tenant OC leakage")

    print("\n10. Telegram disconnect hides tenant OC resources")
    async with GetDB() as db:
        conn = (
            await db.execute(select(TenantTelegramConnection).where(TenantTelegramConnection.tenant_id == t_a.id))
        ).scalar_one()
        conn.active = False
        conn.status = TenantTelegramConnectionStatus.revoked.value
        await db.commit()
    out_disc = await _sub_for(u_ta_id)
    assert "198.51.100.21" not in out_disc
    print("   [x] Disconnected tenant OC excluded")

    print("\n11–12. Native compatibility and distinct panel identities")
    async with GetDB() as db:
        intg3 = OCIntegration(base_url=f"http://{_uid('t4c3')}", api_token_encrypted="e", token_preview="t")
        db.add(intg3)
        await db.flush()
        p1, in1, _, _ = await _oc_panel_with_host(db, intg3.id, "same", None, "198.51.100.31", "Shared Name")
        p2, in2, _, _ = await _oc_panel_with_host(db, intg3.id, "same2", None, "198.51.100.32", "Shared Name")
        nat = ProxyInbound(tag=f"nat_{uuid.uuid4().hex[:6]}")
        db.add(nat)
        await db.flush()
        g = Group(name=_uid("gn"), inbounds=[nat, in1, in2])
        db.add(g)
        await db.flush()
        u = User(
            username=_uid("un"),
            status=UserStatus.active,
            data_limit=0,
            admin_id=admin.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        u.groups = [g]
        db.add(u)
        await db.flush()
        db.add_all([_mapping(u.id, p1.id, ["same"]), _mapping(u.id, p2.id, ["same2"])])
        await db.commit()
        u_dup_id = u.id
        cleanup.extend([intg3, p1, p2, nat, g, u, in1, in2])

    out_dup = await _sub_for(u_dup_id)
    assert "198.51.100.31" in out_dup and "198.51.100.32" in out_dup
    print("   [x] Same remark, different panel addresses both present")

    print("\n=== All Task 4C tests passed ===\n")
    await _cleanup(cleanup, uid)


async def _get_user(user_id: int) -> User:
    async with GetDB() as db:
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


async def _cleanup(entities, primary_user_id: int):
    async with GetDB() as db:
        await db.execute(delete(OCUserMapping).where(OCUserMapping.user_id == primary_user_id))
        for entity in reversed(entities):
            try:
                await db.delete(entity)
            except Exception:
                pass
        await db.commit()


if __name__ == "__main__":
    asyncio.run(run_tests())
