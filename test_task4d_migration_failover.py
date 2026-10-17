"""
Task 4D — panel migration / failover regression tests.
"""
import asyncio
import datetime
import uuid
from unittest.mock import patch

if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc

from sqlalchemy import delete, func, select

from app.db import GetDB
from app.db.crud.group import load_group_attrs
from app.db.models import Admin, Group, ProxyHost, ProxyInbound, Tenant, TenantStatus, User, UserStatus
from app.db.models_oc import (
    OCIntegration,
    OCPanel,
    OCPanelConfig,
    OCUserMapping,
    OCSyncState,
    TenantTelegramConnection,
    TenantTelegramConnectionStatus,
)
from app.jobs.process_oc_sync import process_oc_sync
from app.jobs.reconcile_oc_sync import reconcile_all_oc_users
from app.node.oc_sync import enqueue_oc_user_sync
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.services.oc_subscription_runtime import filter_oc_inbound_tags_for_runtime_subscription
from app.services.oc_user_mapping_state import (
    OC_MAPPING_STATUS_ACTIVE,
    OC_MAPPING_STATUS_DELETED,
    OC_MAPPING_STATUS_PENDING,
    resolve_oc_sync_operation,
)
from app.subscription.config_cache import _cache as sub_cache
from app.subscription.share import generate_subscription


class MockResponse:
    def __init__(self, status=200):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"mock http {self.status}")


def mock_put_ok(*args, **kwargs):
    return MockResponse(200)


def mock_put_fail(*args, **kwargs):
    return MockResponse(500)


def mock_delete_ok(*args, **kwargs):
    return MockResponse(200)


async def mock_decrypt(*args, **kwargs):
    return "decrypted_token"


async def _panel(db, intg_id: int, key: str, address: str, tenant_id: int | None = None):
    panel = OCPanel(
        integration_id=intg_id,
        source_panel_id=f"sp_{key}",
        purchaser_identity="t4d",
        name=f"P {key}",
        tenant_id=tenant_id,
    )
    db.add(panel)
    await db.flush()
    tag = f"oc_{panel.id}_{key}"
    inbound = ProxyInbound(tag=tag)
    db.add(inbound)
    await db.flush()
    host = ProxyHost(
        remark=f"R {key}",
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
        source_config_id=key,
        source_name=f"C {key}",
        virtual_inbound_tag=tag,
        source_missing=False,
        protocol="vless",
        network="tcp",
        port=443,
    )
    db.add(cfg)
    await db.flush()
    return panel, inbound, host, cfg


async def _load_user_for_sync(db, user_id: int) -> User:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    await user.awaitable_attrs.groups
    for group in user.groups:
        await group.awaitable_attrs.inbounds
    return user


async def _sub_text(user_id: int) -> str:
    sub_cache.clear()
    sub_op = SubscriptionOperation(operator_type=OperatorType.API)
    async with GetDB() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        validated = await sub_op.validated_user(user)
        return await generate_subscription(validated, "links", False)


async def _active_mapping_count(db, user_id: int) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(OCUserMapping)
        .where(
            OCUserMapping.user_id == user_id,
            OCUserMapping.status == OC_MAPPING_STATUS_ACTIVE,
            OCUserMapping.last_synced_configs.is_not(None),
        )
    )


async def run_tests():
    print("=== Task 4D Migration / Failover Tests ===\n")
    cleanup: list = []

    async with GetDB() as db:
        admin = (await db.execute(select(Admin).limit(1))).scalar_one()
        admin_id = admin.id

    print("0. resolve_oc_sync_operation after delete uses create")
    deleted_mapping = OCUserMapping(
        user_id=1,
        panel_id=1,
        external_user_id="x",
        status=OC_MAPPING_STATUS_DELETED,
        last_synced_configs=[],
    )
    assert (
        resolve_oc_sync_operation(deleted_mapping, is_new_mapping=False, last_configs=[])
        == "create"
    )
    print("   [x] Re-provision operation is create\n")

    print("1–2. Panel migration with safe overlap")
    async with GetDB() as db:
        intg = OCIntegration(base_url=f"http://t4d-{uuid.uuid4().hex[:6]}", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        pa, in_a, _, ca = await _panel(db, intg.id, "pa", "198.51.100.41")
        pb, in_b, _, cb = await _panel(db, intg.id, "pb", "198.51.100.42")
        group = Group(name=f"g4d_{uuid.uuid4().hex[:8]}", inbounds=[in_a])
        db.add(group)
        await db.flush()
        gid = group.id
        in_a_id = in_a.id
        in_b_id = in_b.id
        pa_id = pa.id
        pb_id = pb.id
        user = User(
            username=f"u4d_{uuid.uuid4().hex[:8]}",
            status=UserStatus.active,
            data_limit=0,
            admin_id=admin_id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
            sub_token=f"tok_{uuid.uuid4().hex[:16]}",
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        uid = user.id
        token = user.sub_token
        await enqueue_oc_user_sync(db, user)
        await db.commit()
        cleanup.extend([intg, pa, pb, group, user, in_a, in_b, ca, cb])

    with patch("aiohttp.ClientSession.put", new=mock_put_ok), patch(
        "app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt
    ):
        await process_oc_sync()
    async with GetDB() as db:
        ma = (
            await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == pa_id)
            )
        ).scalar_one()
        ext_a = ma.external_user_id
        assert ma.status == OC_MAPPING_STATUS_ACTIVE

    async with GetDB() as db:
        group = (await db.execute(select(Group).where(Group.id == gid))).scalar_one()
        await load_group_attrs(group, load_users=False, load_inbounds=True)
        in_a_row = (await db.execute(select(ProxyInbound).where(ProxyInbound.id == in_a_id))).scalar_one()
        in_b_row = (await db.execute(select(ProxyInbound).where(ProxyInbound.id == in_b_id))).scalar_one()
        group.inbounds = [in_a_row, in_b_row]
        user = await _load_user_for_sync(db, uid)
        await enqueue_oc_user_sync(db, user)
        await db.commit()

    sub_overlap = await _sub_text(uid)
    assert "198.51.100.41" in sub_overlap and "198.51.100.42" not in sub_overlap
    print("   [x] Safe overlap: A active, B pending → subscription shows A only")

    for _ in range(5):
        with patch("aiohttp.ClientSession.put", new=mock_put_ok), patch(
            "app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt
        ):
            await process_oc_sync()
    async with GetDB() as db:
        mb = (
            await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == pb_id)
            )
        ).scalar_one()
        assert mb.status == OC_MAPPING_STATUS_ACTIVE
    sub_both = await _sub_text(uid)
    assert "198.51.100.41" in sub_both and "198.51.100.42" in sub_both
    print("   [x] Both panels runtime-active → subscription shows A + B")

    async with GetDB() as db:
        group = (await db.execute(select(Group).where(Group.id == gid))).scalar_one()
        await load_group_attrs(group, load_users=False, load_inbounds=True)
        in_b_row = (await db.execute(select(ProxyInbound).where(ProxyInbound.id == in_b_id))).scalar_one()
        group.inbounds = [in_b_row]
        user = await _load_user_for_sync(db, uid)
        await enqueue_oc_user_sync(db, user)
        await db.commit()

    async with GetDB() as db:
        del_jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id == f"{uid}_{pa_id}",
                    OCSyncState.operation == "delete",
                    OCSyncState.status == "pending",
                )
            )
        ).scalars().all()
        assert len(del_jobs) >= 1
        mb = (
            await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == pb_id)
            )
        ).scalar_one()
        assert mb.status == OC_MAPPING_STATUS_ACTIVE
    print("   [x] B remains healthy while A delete is queued")

    with patch("aiohttp.ClientSession.delete", new=mock_delete_ok), patch(
        "app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt
    ):
        await process_oc_sync()

    async with GetDB() as db:
        ma_after = (
            await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == pa_id)
            )
        ).scalar_one()
        assert ma_after.status == OC_MAPPING_STATUS_DELETED
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        assert user.sub_token == token and user.id == uid
    sub_final = await _sub_text(uid)
    assert "198.51.100.41" not in sub_final and "198.51.100.42" in sub_final
    print("   [x] Migration complete: A removed, B active, identity stable\n")

    print("3. Failed replacement panel does not break desired active panel")
    async with GetDB() as db:
        intg2 = OCIntegration(base_url=f"http://t4d2-{uuid.uuid4().hex[:6]}", api_token_encrypted="e", token_preview="t")
        db.add(intg2)
        await db.flush()
        p1, in1, _, _ = await _panel(db, intg2.id, "f1", "198.51.100.51")
        p2, in2, _, _ = await _panel(db, intg2.id, "f2", "198.51.100.52")
        in1_id = in1.id
        in2_id = in2.id
        g2 = Group(name=f"g4d2_{uuid.uuid4().hex[:8]}", inbounds=[in1])
        db.add(g2)
        await db.flush()
        g2_id = g2.id
        u2 = User(
            username=f"u4d2_{uuid.uuid4().hex[:8]}",
            status=UserStatus.active,
            data_limit=0,
            admin_id=admin_id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        u2.groups = [g2]
        db.add(u2)
        await db.flush()
        u2_id = u2.id
        await enqueue_oc_user_sync(db, u2)
        await db.commit()
        cleanup.extend([intg2, p1, p2, g2, u2, in1, in2])

    with patch("aiohttp.ClientSession.put", new=mock_put_ok), patch(
        "app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt
    ):
        await process_oc_sync()
    async with GetDB() as db:
        g2 = (await db.execute(select(Group).where(Group.id == g2_id))).scalar_one()
        await load_group_attrs(g2, load_users=False, load_inbounds=True)
        in1_row = (await db.execute(select(ProxyInbound).where(ProxyInbound.id == in1_id))).scalar_one()
        in2_row = (await db.execute(select(ProxyInbound).where(ProxyInbound.id == in2_id))).scalar_one()
        g2.inbounds = [in1_row, in2_row]
        u2 = await _load_user_for_sync(db, u2_id)
        await enqueue_oc_user_sync(db, u2)
        await db.commit()
    with patch("aiohttp.ClientSession.put", new=mock_put_fail), patch(
        "app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt
    ):
        await process_oc_sync()
    sub_fail = await _sub_text(u2_id)
    assert "198.51.100.51" in sub_fail and "198.51.100.52" not in sub_fail
    print("   [x] B failed provision; A still in subscription\n")

    print("5. Duplicate enqueue retains single mapping identity")
    async with GetDB() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        for _ in range(3):
            await enqueue_oc_user_sync(db, user)
        await db.commit()
    async with GetDB() as db:
        maps = (
            await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == pb_id))
        ).scalars().all()
        assert len(maps) == 1
        assert maps[0].external_user_id != ext_a
    print("   [x] No duplicate mappings / stable external_user_id per panel\n")

    print("6. Reconciliation recovers pending migration")
    async with GetDB() as db:
        mb = (
            await db.execute(
                select(OCUserMapping).where(OCUserMapping.user_id == uid, OCUserMapping.panel_id == pb_id)
            )
        ).scalar_one()
        mb.status = OC_MAPPING_STATUS_PENDING
        mb.last_synced_configs = None
        await db.execute(delete(OCSyncState).where(OCSyncState.entity_id == f"{uid}_{pb_id}"))
        await db.commit()
    await reconcile_all_oc_users()
    async with GetDB() as db:
        pending = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id == f"{uid}_{pb_id}",
                    OCSyncState.status == "pending",
                )
            )
        ).scalars().all()
        assert len(pending) >= 1
    print("   [x] Reconcile re-queues drift after lost job\n")

    print("7. Telegram disconnect: delete allowed, create blocked")
    async with GetDB() as db:
        tenant = Tenant(name=f"t4d-tg-{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        intg_t = OCIntegration(base_url=f"http://t4d-tg", api_token_encrypted="e", token_preview="t")
        db.add(intg_t)
        await db.flush()
        pt, in_t, _, ct = await _panel(db, intg_t.id, "tg", "198.51.100.61", tenant_id=tenant.id)
        db.add(
            TenantTelegramConnection(
                tenant_id=tenant.id,
                status=TenantTelegramConnectionStatus.revoked.value,
                active=False,
            )
        )
        await db.flush()
        u3 = User(
            username=f"u4d-tg_{uuid.uuid4().hex[:8]}",
            status=UserStatus.active,
            data_limit=0,
            admin_id=admin_id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        g3 = Group(name=f"g4d-tg_{uuid.uuid4().hex[:8]}", inbounds=[in_t])
        db.add(g3)
        await db.flush()
        u3.groups = [g3]
        db.add(u3)
        await db.flush()
        m = OCUserMapping(
            user_id=u3.id,
            panel_id=pt.id,
            external_user_id=str(uuid.uuid4()),
            status=OC_MAPPING_STATUS_ACTIVE,
            last_synced_configs=["tg"],
        )
        db.add(m)
        await db.flush()
        u3_id = u3.id
        await load_group_attrs(g3, load_users=False, load_inbounds=True)
        g3.inbounds = []
        await enqueue_oc_user_sync(db, u3)
        await db.commit()
        cleanup.extend([tenant, intg_t, pt, g3, u3, in_t, ct])

    async with GetDB() as db:
        create_jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id == f"{u3_id}_{pt.id}",
                    OCSyncState.operation.in_(["create", "update"]),
                    OCSyncState.status == "pending",
                )
            )
        ).scalars().all()
        assert create_jobs == []
        del_jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id == f"{u3_id}_{pt.id}",
                    OCSyncState.operation == "delete",
                )
            )
        ).scalars().all()
        assert len(del_jobs) >= 1
    print("   [x] Disconnect blocks provision; cleanup delete still queued\n")

    print("8. Tenant-scoped desired tags filtered for subscription")
    async with GetDB() as db:
        t_a = Tenant(name=f"t4d-a-{uuid.uuid4().hex[:6]}", status=TenantStatus.active)
        t_b = Tenant(name=f"t4d-b-{uuid.uuid4().hex[:6]}", status=TenantStatus.active)
        db.add_all([t_a, t_b])
        await db.flush()
        role_id = admin.role_id
        adm_a = Admin(
            username=f"adm4d-a-{uuid.uuid4().hex[:6]}",
            hashed_password=admin.hashed_password,
            tenant_id=t_a.id,
            role_id=role_id,
        )
        db.add(adm_a)
        await db.flush()
        intg_x = OCIntegration(base_url=f"http://t4d-x", api_token_encrypted="e", token_preview="t")
        db.add(intg_x)
        await db.flush()
        pta, in_ta, _, _ = await _panel(db, intg_x.id, "ta", "198.51.100.71", tenant_id=t_a.id)
        ptb, in_tb, _, _ = await _panel(db, intg_x.id, "tb", "198.51.100.72", tenant_id=t_b.id)
        for t in (t_a, t_b):
            db.add(
                TenantTelegramConnection(
                    tenant_id=t.id,
                    telegram_user_id=1,
                    oc_account_id=1,
                    status=TenantTelegramConnectionStatus.active.value,
                    active=True,
                )
            )
        await db.flush()
        gx = Group(name=f"gx_{uuid.uuid4().hex[:6]}", inbounds=[in_ta, in_tb])
        db.add(gx)
        await db.flush()
        ux = User(
            username=f"ux_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            data_limit=0,
            admin_id=adm_a.id,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        ux.groups = [gx]
        db.add(ux)
        await db.flush()
        db.add_all(
            [
                OCUserMapping(
                    user_id=ux.id,
                    panel_id=pta.id,
                    external_user_id=str(uuid.uuid4()),
                    status=OC_MAPPING_STATUS_ACTIVE,
                    last_synced_configs=["ta"],
                ),
                OCUserMapping(
                    user_id=ux.id,
                    panel_id=ptb.id,
                    external_user_id=str(uuid.uuid4()),
                    status=OC_MAPPING_STATUS_ACTIVE,
                    last_synced_configs=["tb"],
                ),
            ]
        )
        await db.commit()
        ux_id = ux.id
        in_ta_tag = in_ta.tag
        in_tb_tag = in_tb.tag
        cleanup.extend([t_a, t_b, adm_a, intg_x, pta, ptb, gx, ux, in_ta, in_tb])

    async with GetDB() as db:
        user = (await db.execute(select(User).where(User.id == ux_id))).scalar_one()
        inbounds = await user.inbounds()
        oc_tags = [t for t in inbounds if t.startswith("oc_")]
        filtered = await filter_oc_inbound_tags_for_runtime_subscription(db, ux_id, oc_tags, t_a.id)
        assert in_ta_tag in filtered
        assert in_tb_tag not in filtered
    print("   [x] Cross-tenant panel tag excluded from runtime subscription\n")

    print("11. Native user without OC mappings unchanged")
    async with GetDB() as db:
        nat = ProxyInbound(tag=f"nat4d_{uuid.uuid4().hex[:6]}")
        db.add(nat)
        await db.flush()
        gn = Group(name=f"gn4d_{uuid.uuid4().hex[:6]}", inbounds=[nat])
        db.add(gn)
        await db.flush()
        un = User(
            username=f"un4d_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            data_limit=0,
            admin_id=admin_id,
        )
        un.groups = [gn]
        db.add(un)
        await db.flush()
        await enqueue_oc_user_sync(db, un)
        await db.commit()
        un_id = un.id
        cleanup.extend([nat, gn, un])

    async with GetDB() as db:
        n = await db.scalar(select(func.count()).select_from(OCUserMapping).where(OCUserMapping.user_id == un_id))
        assert n == 0
    print("   [x] Native-only user has zero OC mappings\n")

    print("12. Transaction rollback does not leave OC sync jobs")
    async with GetDB() as db:
        before = await db.scalar(select(func.count()).select_from(OCSyncState))
    async with GetDB() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        await enqueue_oc_user_sync(db, user)
        await db.rollback()
    async with GetDB() as db:
        after = await db.scalar(select(func.count()).select_from(OCSyncState))
        assert after == before
    print("   [x] Rolled-back enqueue leaves queue unchanged\n")

    print("=== All Task 4D tests passed ===\n")

    async with GetDB() as db:
        await db.execute(delete(OCSyncState))
        for entity in reversed(cleanup):
            try:
                await db.delete(entity)
            except Exception:
                pass
        await db.commit()


if __name__ == "__main__":
    asyncio.run(run_tests())
