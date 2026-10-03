"""
Task 4B — Group-driven OC sync desired vs runtime-active lifecycle.
"""
import asyncio
import datetime
import uuid
from unittest.mock import patch

import aiohttp

if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc

from sqlalchemy import delete, select

from app.db import GetDB
from app.db.models import Admin, Group, ProxyInbound, User
from app.db.models_oc import OCIntegration, OCPanel, OCPanelConfig, OCUserMapping, OCSyncState
from app.jobs.process_oc_sync import process_oc_sync
from app.jobs.reconcile_oc_sync import reconcile_all_oc_users
from app.db.crud.group import load_group_attrs
from app.node.oc_sync import enqueue_oc_user_sync
from app.node.user import _serialize_user_for_node, serialize_user
from app.services.oc_user_mapping_state import OC_MAPPING_STATUS_PENDING


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


def mock_put_success(*args, **kwargs):
    return MockResponse(200)


def mock_put_failure(*args, **kwargs):
    return MockResponse(500)


async def mock_decrypt(*args, **kwargs):
    return "decrypted_token"


async def _panel_setup(db, admin_id: int, panel_suffix: str):
    integration = OCIntegration(
        base_url="http://test4b", api_token_encrypted="enc", token_preview="t4b"
    )
    db.add(integration)
    await db.flush()
    panel = OCPanel(
        integration_id=integration.id,
        source_panel_id=f"sp_{panel_suffix}",
        purchaser_identity="t4b",
        name=f"Panel {panel_suffix}",
    )
    db.add(panel)
    await db.flush()
    tag = f"oc_{panel.id}_{panel_suffix}"
    inbound = ProxyInbound(tag=tag)
    db.add(inbound)
    await db.flush()
    config = OCPanelConfig(
        panel_id=panel.id,
        source_config_id=f"cfg_{panel_suffix}",
        source_name=f"Cfg {panel_suffix}",
        virtual_inbound_tag=tag,
    )
    db.add(config)
    await db.flush()
    return integration, panel, inbound, config


async def run_tests():
    print("=== Task 4B OC Sync Lifecycle Tests ===\n")
    admin_id = None
    entities = []
    mapping_id = None
    group_id = None
    panel2_id = None

    async with GetDB() as db:
        admin_id = (await db.execute(select(Admin).limit(1))).scalar_one().id

    print("1. Group → panel desired state queues async OC sync (pending mapping)")
    async with GetDB() as db:
        intg, panel, inbound, cfg = await _panel_setup(db, admin_id, "a")
        group = Group(name=f"g4b_{uuid.uuid4().hex[:8]}", inbounds=[inbound])
        db.add(group)
        await db.flush()
        group_id = group.id
        user = User(
            username=f"u4b_{uuid.uuid4().hex[:8]}",
            status="active",
            data_limit=0,
            admin_id=admin_id,
            sub_token=f"stable_sub_token_4b_{uuid.uuid4().hex[:12]}",
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        sub_before = user.sub_token
        await enqueue_oc_user_sync(db, user)
        await db.commit()
        user_id = user.id
        panel_id = panel.id
        entities.extend([intg, panel, inbound, cfg, group, user])

    async with GetDB() as db:
        mapping = (
            await db.execute(
                select(OCUserMapping).where(
                    OCUserMapping.user_id == user_id, OCUserMapping.panel_id == panel_id
                )
            )
        ).scalar_one()
        mapping_id = mapping.id
        assert mapping.status == OC_MAPPING_STATUS_PENDING
        assert mapping.last_synced_configs is None
        jobs = (
            await db.execute(
                select(OCSyncState).where(OCSyncState.entity_id == f"{user_id}_{panel_id}")
            )
        ).scalars().all()
        assert len(jobs) == 1 and jobs[0].operation == "create"
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        proto = await serialize_user(user)
        assert not any(p.email.endswith(f"_p{panel_id}") for p in proto)
    print("   [x] Pending mapping + create job; no runtime panel identity\n")

    print("2. Successful worker activation enables runtime panel route")
    async with GetDB() as db:
        job = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id == f"{user_id}_{panel_id}", OCSyncState.status == "pending"
                )
            )
        ).scalar_one()
    with patch("aiohttp.ClientSession.put", new=mock_put_success), patch(
        "app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt
    ):
        await process_oc_sync()

    async with GetDB() as db:
        mapping = (
            await db.execute(select(OCUserMapping).where(OCUserMapping.id == mapping_id))
        ).scalar_one()
        assert mapping.status == "active"
        assert mapping.last_synced_configs == [f"cfg_a"]
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        assert user.sub_token == sub_before
        proto = await serialize_user(user)
        panel_proto = next(p for p in proto if p.email == f"{user_id}_p{panel_id}")
        assert inbound.tag in panel_proto.inbounds
    print("   [x] Active mapping + runtime serialization after successful PUT\n")

    print("3. Group inbound change moves desired panel → delete old + create new")
    async with GetDB() as db:
        intg2, panel2, inbound2, cfg2 = await _panel_setup(db, admin_id, "b")
        group = (await db.execute(select(Group).where(Group.id == group_id))).scalar_one()
        await load_group_attrs(group, load_users=False, load_inbounds=True)
        group.inbounds = [inbound2]
        await db.flush()
        panel2_id = panel2.id
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        await enqueue_oc_user_sync(db, user)
        await db.commit()
        entities.extend([intg2, panel2, inbound2, cfg2])

    async with GetDB() as db:
        delete_jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id == f"{user_id}_{panel_id}",
                    OCSyncState.operation == "delete",
                )
            )
        ).scalars().all()
        assert len(delete_jobs) >= 1
        create_jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_id == f"{user_id}_{panel2_id}",
                    OCSyncState.operation == "create",
                )
            )
        ).scalars().all()
        assert len(create_jobs) >= 1
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        assert user.sub_token == sub_before
    print("   [x] Panel migration queued without changing PasarGuard user identity\n")

    print("4. Failed provisioning does not expose new panel at runtime")
    with patch("aiohttp.ClientSession.put", new=mock_put_failure), patch(
        "app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt
    ):
        await process_oc_sync()
    async with GetDB() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        proto = await serialize_user(user)
        assert not any(p.email.endswith(f"_p{panel2_id}") for p in proto)
    print("   [x] Failed PUT leaves panel non-runtime\n")

    print("5. Reconciliation retries and eventually activates panel")
    with patch("aiohttp.ClientSession.put", new=mock_put_success), patch(
        "app.jobs.process_oc_sync.decrypt_secret", new=mock_decrypt
    ):
        await reconcile_all_oc_users()
        await process_oc_sync()
    async with GetDB() as db:
        mapping2 = (
            await db.execute(
                select(OCUserMapping).where(
                    OCUserMapping.user_id == user_id, OCUserMapping.panel_id == panel2_id
                )
            )
        ).scalar_one()
        assert mapping2.status == "active"
        assert mapping2.last_synced_configs == [f"cfg_b"]
    print("   [x] Reconcile + worker recovery\n")

    print("6. Native-only user unchanged (no OC mappings)")
    async with GetDB() as db:
        native_inbound = ProxyInbound(tag=f"native4b_{uuid.uuid4().hex[:8]}")
        db.add(native_inbound)
        await db.flush()
        group_n = Group(name=f"gn4b_{uuid.uuid4().hex[:8]}", inbounds=[native_inbound])
        db.add(group_n)
        await db.flush()
        user_n = User(
            username=f"native4b_{uuid.uuid4().hex[:8]}",
            status="active",
            data_limit=0,
            admin_id=admin_id,
        )
        user_n.groups = [group_n]
        db.add(user_n)
        await db.flush()
        await enqueue_oc_user_sync(db, user_n)
        await db.commit()
        uid_n = user_n.id
        entities.extend([native_inbound, group_n, user_n])

    async with GetDB() as db:
        mappings = (
            await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == uid_n))
        ).scalars().all()
        assert mappings == []
        proto = _serialize_user_for_node(
            uid_n, {"shadowsocks": {"password": "x", "method": "chacha20-ietf-poly1305"}}, [native_inbound.tag]
        )
        assert len(proto) == 1 and proto[0].email == str(uid_n)
    print("   [x] Native user has no OC side effects\n")

    print("7. Deleted mapping does not leak into runtime serialization")
    async with GetDB() as db:
        stale = (
            await db.execute(
                select(OCUserMapping).where(
                    OCUserMapping.user_id == user_id, OCUserMapping.panel_id == panel_id
                )
            )
        ).scalar_one()
        stale.status = "deleted"
        stale.last_synced_configs = []
        await db.commit()
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        proto = await serialize_user(user)
        assert not any(p.email.endswith(f"_p{panel_id}") for p in proto)
    print("   [x] Soft-deleted mapping excluded from runtime\n")

    print("=== All Task 4B tests passed ===\n")

    async with GetDB() as db:
        await db.execute(delete(OCSyncState))
        await db.execute(delete(OCUserMapping).where(OCUserMapping.user_id.in_([user_id, uid_n])))
        for entity in reversed(entities):
            try:
                await db.delete(entity)
            except Exception:
                pass
        await db.commit()


if __name__ == "__main__":
    asyncio.run(run_tests())
