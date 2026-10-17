"""
Regression tests for the PasarGuard <-> Outbound Center membership/host/subscription defects.

Confirmed root causes covered here:

1. ``remove_old_inbounds`` deleted every OC virtual inbound (they are not in the Xray core config).
   That cleared Group membership, orphaned hosts / destination rows (NULL tags) and made the OC
   reconcile job delete the provisioned user (configs stopped responding minutes after a save).
2. Host deletion left stale Group membership when the host was orphaned or native.
3. Destination hosts were resolved to the customer's links through remark text only; stylized
   Unicode remarks normalize to an empty key and were silently dropped (77 -> 27).

These tests use the application's normal database fixtures; run them ONLY against an isolated
database (never the production ``SQLALCHEMY_DATABASE_URL``).
"""
from __future__ import annotations

import base64
import json
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.crud.host import get_inbounds_not_in_tags, remove_inbounds
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
    inbounds_groups_association,
)
from app.db.models_oc import OCIntegration, OCPanel, OCPanelDestinationHost, OCSyncState, OCUserMapping
from app.jobs.inbound import remove_stale_inbounds
from app.node.oc_sync import enqueue_oc_user_sync
from app.services.oc_destination_host_lifecycle import (
    delete_hosts_and_detach_membership,
)
from app.services.oc_panel_destination_hosts import (
    ensure_panel_destination_hosts_materialized,
    reconcile_panel_destination_hosts,
)
from app.services.oc_share_link import destination_virtual_inbound_tag, share_link_structural_key
from app.services.oc_user_subscription_hosts import collect_oc_subscription_links_for_user

CORE_TAGS = ["Core Inbound A"]

# Stylized Unicode remarks: ``_normalize_key`` (ASCII only) turns these into an empty string.
STYLIZED = [
    "🇫🇷 𝗙𝗥𝗔𝗡𝗖𝗘 - 𝗠𝗖𝗜",
    "🇫🇷 𝗙𝗥𝗔𝗡𝗖𝗘 - 𝗨𝗟𝗧𝗥𝗔",
    "🇩🇪 𝗚𝗘𝗥𝗠𝗔𝗡𝗬 - 𝗩𝗜𝗣",
    "🇩🇪 𝗚𝗘𝗥𝗠𝗔𝗡𝗬 - 𝗠𝗖𝗜",
    "🇳🇱 𝗡𝗘𝗧𝗛𝗘𝗥𝗟𝗔𝗡𝗗𝗦",
]


def _link(uid: str, host: str, port: int, remark: str, extra: str = "") -> str:
    from urllib.parse import quote

    return f"vless://{uid}@{host}:{port}?encryption=none&security=tls&type=ws&path=%2Fws{extra}#{quote(remark)}"


async def _tenant_admin(db):
    tenant = Tenant(name=f"mr_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
    db.add(tenant)
    await db.flush()
    role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
    admin = Admin(username=f"mr_{uuid.uuid4().hex[:6]}", hashed_password="x", role_id=role.id, tenant_id=tenant.id)
    db.add(admin)
    await db.flush()
    await create_workspace_for_administrator(db, admin)
    await db.refresh(admin)
    return tenant, admin


async def _panel(db, tenant, admin, links: list[str], *, name="P"):
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


async def _group_with_tags(db, tenant, admin, tags: list[str], *, name=None) -> Group:
    inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(tags)))).scalars().all()
    assert len(inbounds) == len(tags)
    group = Group(
        name=name or f"g_{uuid.uuid4().hex[:6]}",
        inbounds=list(inbounds),
        tenant_id=tenant.id,
        workspace_id=admin.workspace_id,
    )
    db.add(group)
    await db.flush()
    return group


async def _membership(db, group_id: int) -> set[str]:
    rows = await db.execute(
        select(ProxyInbound.tag)
        .join(inbounds_groups_association, inbounds_groups_association.c.inbound_id == ProxyInbound.id)
        .where(inbounds_groups_association.c.group_id == group_id)
    )
    return set(rows.scalars().all())


def _tags(panel, rows) -> list[str]:
    return [destination_virtual_inbound_tag(panel.id, r.destination_config_id) for r in rows]


async def _old_job_behaviour(db) -> list[str]:
    """What ``remove_old_inbounds`` did before the fix: delete every inbound not in the core config."""
    old = await get_inbounds_not_in_tags(db, CORE_TAGS)
    tags = [i.tag for i in old]
    await remove_inbounds(db, old)
    return tags


# --------------------------------------------------------------------------------------
# 1. Root cause: core-config inbound cleanup vs OC virtual inbounds
# --------------------------------------------------------------------------------------


async def test_unprotected_cleanup_cleared_membership_and_orphaned_hosts():
    """Documents the confirmed defect: the old cleanup zeroed membership and NULLed the FKs."""
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, [_link("u1", "a.example", 443, "One"), _link("u2", "b.example", 443, "Two")])
        group = await _group_with_tags(db, tenant, admin, _tags(panel, rows))
        await db.commit()
        group_id, panel_id, ws = group.id, panel.id, admin.workspace_id
        assert len(await _membership(db, group_id)) == 2

        removed = await _old_job_behaviour(db)
        assert set(removed) >= set(_tags(panel, rows))

    async with GetDB() as db:
        assert await _membership(db, group_id) == set()
        dests = (await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel_id))).scalars().all()
        assert all(d.virtual_inbound_tag is None for d in dests)
        hosts = (await db.execute(select(ProxyHost).where(ProxyHost.workspace_id == ws))).scalars().all()
        assert hosts and all(h.inbound_tag is None for h in hosts)


async def test_cleanup_job_preserves_oc_virtual_inbounds_membership_and_host_links():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, [_link("u1", "a.example", 443, "One"), _link("u2", "b.example", 443, "Two")])
        native_stale = ProxyInbound(tag=f"stale_native_{uuid.uuid4().hex[:6]}")
        db.add(native_stale)
        await db.flush()
        tags = _tags(panel, rows)
        group = await _group_with_tags(db, tenant, admin, tags)
        await db.commit()
        group_id, stale_tag = group.id, native_stale.tag

    # Repeated scheduler cycles (every 10 minutes in production) and "UI reloads" in between.
    for cycle in range(3):
        async with GetDB() as db:
            removed = await remove_stale_inbounds(db, CORE_TAGS)
            assert (stale_tag in removed) == (cycle == 0)  # stale native inbound removed once
            assert not set(removed) & set(tags)
        async with GetDB() as db:
            assert await _membership(db, group_id) == set(tags)
            dests = (await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))).scalars().all()
            assert {d.virtual_inbound_tag for d in dests} == set(tags)
            hosts = (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag.in_(tags)))).scalars().all()
            assert len(hosts) == 2


async def test_cleanup_job_removes_orphaned_oc_inbounds_of_deleted_panels():
    """Protection is ownership based: an ``oc_`` inbound with no owning OC row is still stale."""
    async with GetDB() as db:
        orphan = ProxyInbound(tag=f"oc_999999_d_{uuid.uuid4().hex[:8]}")
        db.add(orphan)
        await db.commit()
        tag = orphan.tag
    async with GetDB() as db:
        removed = await remove_stale_inbounds(db, CORE_TAGS)
        assert tag in removed


async def test_cleanup_job_skips_when_core_inbound_list_is_empty():
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        extra = ProxyInbound(tag=f"keep_{uuid.uuid4().hex[:6]}")
        db.add(extra)
        group = Group(name=f"g_{uuid.uuid4().hex[:6]}", inbounds=[extra], tenant_id=tenant.id, workspace_id=admin.workspace_id)
        db.add(group)
        await db.commit()
        gid, tag = group.id, extra.tag
    async with GetDB() as db:
        assert await remove_stale_inbounds(db, []) == []
    async with GetDB() as db:
        assert await _membership(db, gid) == {tag}


# --------------------------------------------------------------------------------------
# 2. Synchronization: partial / empty / repeated source responses never clear membership
# --------------------------------------------------------------------------------------


async def test_panel_resync_partial_empty_and_repeated_preserves_membership():
    links = [_link(f"u{i}", f"h{i}.example", 443 + i, f"Cfg {i}") for i in range(4)]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, links)
        tags = _tags(panel, rows)
        group = await _group_with_tags(db, tenant, admin, tags)
        await db.commit()
        gid, pid = group.id, panel.id

    async with GetDB() as db:
        panel = await db.get(OCPanel, pid)
        # Empty source response: must be a no-op, not "delete everything".
        assert await reconcile_panel_destination_hosts(db, panel, []) == 0
        # Partial response: missing rows are marked source_missing, never deleted from the group.
        await reconcile_panel_destination_hosts(db, panel, links[:2])
        # Repeated identical syncs and re-materialization.
        await reconcile_panel_destination_hosts(db, panel, links)
        await reconcile_panel_destination_hosts(db, panel, links)
        await ensure_panel_destination_hosts_materialized(db, panel)
        await db.commit()

    async with GetDB() as db:
        assert await _membership(db, gid) == set(tags)
        dests = (await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == pid))).scalars().all()
        assert len(dests) == 4 and not any(d.source_missing for d in dests)


async def test_partial_source_response_keeps_membership_of_missing_rows():
    links = [_link(f"u{i}", f"h{i}.example", 443 + i, f"Cfg {i}") for i in range(3)]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, links)
        tags = _tags(panel, rows)
        group = await _group_with_tags(db, tenant, admin, tags)
        await reconcile_panel_destination_hosts(db, panel, links[:1])
        await db.commit()
        gid, pid = group.id, panel.id
    async with GetDB() as db:
        assert await _membership(db, gid) == set(tags)
        missing = (
            await db.execute(
                select(OCPanelDestinationHost).where(
                    OCPanelDestinationHost.panel_id == pid, OCPanelDestinationHost.source_missing.is_(True)
                )
            )
        ).scalars().all()
        assert len(missing) == 2


async def test_rematerialization_adopts_orphaned_host_without_duplicates():
    """Legacy damage (inbound removed -> host/dest tag NULL) is repaired in place, not duplicated."""
    links = [_link("u1", "a.example", 443, "🇫🇷 𝗙𝗥𝗔𝗡𝗖𝗘"), _link("u2", "b.example", 443, "Plain")]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, links)
        await db.commit()
        pid, ws = panel.id, admin.workspace_id
        host_count = len((await db.execute(select(ProxyHost).where(ProxyHost.workspace_id == ws))).scalars().all())
        assert host_count == 2
        await _old_job_behaviour(db)  # legacy damage

    async with GetDB() as db:
        assert all(h.inbound_tag is None for h in (await db.execute(select(ProxyHost).where(ProxyHost.workspace_id == ws))).scalars().all())
        panel = await db.get(OCPanel, pid)
        await ensure_panel_destination_hosts_materialized(db, panel)
        await db.commit()
    async with GetDB() as db:
        hosts = (await db.execute(select(ProxyHost).where(ProxyHost.workspace_id == ws))).scalars().all()
        assert len(hosts) == host_count
        assert all(h.inbound_tag and h.inbound_tag.startswith(f"oc_{pid}_d_") for h in hosts)


# --------------------------------------------------------------------------------------
# 3. Host deletion keeps group membership consistent
# --------------------------------------------------------------------------------------


async def test_deleting_oc_destination_host_detaches_only_that_membership():
    links = [_link(f"u{i}", f"h{i}.example", 443, f"Cfg {i}") for i in range(3)]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, links)
        tags = _tags(panel, rows)
        group = await _group_with_tags(db, tenant, admin, tags)
        other = await _group_with_tags(db, tenant, admin, tags[1:])
        await db.commit()
        gid, oid = group.id, other.id
        victim = (await db.execute(select(ProxyHost).where(ProxyHost.inbound_tag == tags[0]))).scalar_one()
        stats = await delete_hosts_and_detach_membership(db, [victim])
        await db.commit()
        assert stats["oc_teardown"] == 1
    async with GetDB() as db:
        assert await _membership(db, gid) == set(tags[1:])
        assert await _membership(db, oid) == set(tags[1:])
        left = (await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))).scalars().all()
        assert len(left) == 2


async def test_deleting_orphaned_oc_host_removes_stale_membership_when_unambiguous():
    links = [_link("u1", "a.example", 443, "Alpha"), _link("u2", "b.example", 443, "Beta")]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, links)
        tags = _tags(panel, rows)
        group = await _group_with_tags(db, tenant, admin, tags)
        await db.commit()
        gid, ws = group.id, admin.workspace_id
        # Legacy damage: inbound deleted, then re-created by a group save -> host stays orphaned.
        await _old_job_behaviour(db)
    async with GetDB() as db:
        for tag in tags:
            db.add(ProxyInbound(tag=tag))
        await db.flush()
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(tags)))).scalars().all()
        grp = await db.get(Group, gid, options=[selectinload(Group.inbounds)])
        grp.inbounds = list(inbounds)
        await db.commit()
        assert await _membership(db, gid) == set(tags)

        orphan = (await db.execute(select(ProxyHost).where(ProxyHost.workspace_id == ws, ProxyHost.remark == "Alpha"))).scalar_one()
        assert orphan.inbound_tag is None
        stats = await delete_hosts_and_detach_membership(db, [orphan])
        await db.commit()
        assert stats["orphan_teardown"] == 1
    async with GetDB() as db:
        remaining = await _membership(db, gid)
        assert len(remaining) == 1 and remaining != set(tags)


async def test_deleting_ambiguous_orphan_does_not_touch_other_rows():
    """Two destination rows with the same display name -> never guess which one was deleted."""
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        p1, r1 = await _panel(db, tenant, admin, [_link("u1", "a.example", 443, "Same")], name="P1")
        p2, r2 = await _panel(db, tenant, admin, [_link("u2", "b.example", 443, "Same")], name="P2")
        tags = _tags(p1, r1) + _tags(p2, r2)
        group = await _group_with_tags(db, tenant, admin, tags)
        await db.commit()
        gid, ws = group.id, admin.workspace_id
        await _old_job_behaviour(db)
    async with GetDB() as db:
        for tag in tags:
            db.add(ProxyInbound(tag=tag))
        await db.flush()
        inbounds = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag.in_(tags)))).scalars().all()
        grp = await db.get(Group, gid, options=[selectinload(Group.inbounds)])
        grp.inbounds = list(inbounds)
        await db.commit()
        orphan = (await db.execute(select(ProxyHost).where(ProxyHost.workspace_id == ws).order_by(ProxyHost.id))).scalars().first()
        stats = await delete_hosts_and_detach_membership(db, [orphan])
        await db.commit()
        assert stats["orphan_teardown"] == 0
    async with GetDB() as db:
        assert await _membership(db, gid) == set(tags)


async def test_deleting_last_native_host_detaches_tag_only_inside_its_scope():
    tag = f"native_{uuid.uuid4().hex[:8]}"
    async with GetDB() as db:
        t1, a1 = await _tenant_admin(db)
        t2, a2 = await _tenant_admin(db)
        inbound = ProxyInbound(tag=tag)
        db.add(inbound)
        await db.flush()
        h1 = ProxyHost(remark="h1", priority=0, address={"x.example"}, port=443, path=None, allowinsecure=None, alpn=[], status=[])
        h1.inbound, h1.workspace_id, h1.tenant_id = inbound, a1.workspace_id, t1.id
        h1b = ProxyHost(remark="h1b", priority=1, address={"y.example"}, port=443, path=None, allowinsecure=None, alpn=[], status=[])
        h1b.inbound, h1b.workspace_id, h1b.tenant_id = inbound, a1.workspace_id, t1.id
        h2 = ProxyHost(remark="h2", priority=0, address={"z.example"}, port=443, path=None, allowinsecure=None, alpn=[], status=[])
        h2.inbound, h2.workspace_id, h2.tenant_id = inbound, a2.workspace_id, t2.id
        db.add_all([h1, h1b, h2])
        g1 = Group(name=f"g1_{uuid.uuid4().hex[:5]}", inbounds=[inbound], tenant_id=t1.id, workspace_id=a1.workspace_id)
        g2 = Group(name=f"g2_{uuid.uuid4().hex[:5]}", inbounds=[inbound], tenant_id=t2.id, workspace_id=a2.workspace_id)
        db.add_all([g1, g2])
        await db.commit()
        g1id, g2id = g1.id, g2.id

        # Another host with the same tag remains in the scope: membership is kept.
        stats = await delete_hosts_and_detach_membership(db, [h1])
        await db.commit()
        assert stats["membership_detached"] == 0
        assert await _membership(db, g1id) == {tag}

        # Last host in scope deleted: only this workspace's group loses the tag.
        stats = await delete_hosts_and_detach_membership(db, [h1b])
        await db.commit()
        assert stats["membership_detached"] == 1
    async with GetDB() as db:
        assert await _membership(db, g1id) == set()
        assert await _membership(db, g2id) == {tag}


# --------------------------------------------------------------------------------------
# 4. Subscription: every eligible selected config is rendered (endpoint identity)
# --------------------------------------------------------------------------------------


def test_structural_key_ignores_credential_and_remark_but_not_endpoint():
    a = _link("uuid-A", "h.example", 443, "𝗙𝗥𝗔𝗡𝗖𝗘")
    b = _link("uuid-B", "h.example", 443, "anything else")
    c = _link("uuid-A", "h.example", 444, "𝗙𝗥𝗔𝗡𝗖𝗘")
    d = _link("uuid-A", "other.example", 443, "𝗙𝗥𝗔𝗡𝗖𝗘")
    assert share_link_structural_key(a) == share_link_structural_key(b)
    assert share_link_structural_key(a) != share_link_structural_key(c)
    assert share_link_structural_key(a) != share_link_structural_key(d)

    def vmess(uid: str, ps: str) -> str:
        body = {"v": "2", "ps": ps, "add": "v.example", "port": "443", "id": uid, "net": "ws", "tls": "tls"}
        return "vmess://" + base64.b64encode(json.dumps(body).encode()).decode()

    assert share_link_structural_key(vmess("1", "x")) == share_link_structural_key(vmess("2", "y"))
    ss1 = "ss://" + base64.urlsafe_b64encode(b"aes-256-gcm:pw1").decode().rstrip("=") + "@s.example:8388#a"
    ss2 = "ss://" + base64.urlsafe_b64encode(b"aes-256-gcm:pw2").decode().rstrip("=") + "@s.example:8388#b"
    assert share_link_structural_key(ss1) == share_link_structural_key(ss2)
    assert share_link_structural_key("not a link") is None


def _active_connection():
    """Tenant-scoped panels need an active Telegram connection; that gate is not under test here."""
    return patch(
        "app.services.oc_subscription_runtime.panel_has_active_connection",
        new=AsyncMock(return_value=True),
    )


async def _user_with_mapping(db, admin, group, panels):
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
    for panel in panels:
        db.add(
            OCUserMapping(
                user_id=user.id,
                panel_id=panel.id,
                external_user_id=str(uuid.uuid4()),
                status="active",
                last_synced_configs=["c"],
                external_subscription_url="https://1.1.1.1/sub/x",
            )
        )
    await db.flush()
    return user


async def test_subscription_renders_all_selected_configs_across_panels_with_stylized_and_duplicate_remarks():
    # Panel 1: stylized remarks (empty normalized key). Panel 2 reuses one remark from panel 1
    # for a different endpoint, plus a duplicate remark inside the same panel.
    disc1 = [_link(f"d{i}", f"p1h{i}.example", 443, r) for i, r in enumerate(STYLIZED)]
    disc2 = [
        _link("d9", "p2a.example", 443, STYLIZED[0]),
        _link("d10", "p2b.example", 443, "Dup"),
        _link("d11", "p2c.example", 443, "Dup"),
    ]
    cust1 = [_link(f"c{i}", f"p1h{i}.example", 443, r) for i, r in enumerate(STYLIZED)]
    cust2 = [
        _link("c9", "p2a.example", 443, STYLIZED[0]),
        _link("c10", "p2b.example", 443, "Dup"),
        _link("c11", "p2c.example", 443, "Dup"),
    ]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        p1, r1 = await _panel(db, tenant, admin, disc1, name="P1")
        p2, r2 = await _panel(db, tenant, admin, disc2, name="P2")
        tags = _tags(p1, r1) + _tags(p2, r2)
        assert len(tags) == 8
        group = await _group_with_tags(db, tenant, admin, tags)
        user = await _user_with_mapping(db, admin, group, [p1, p2])
        await db.commit()
        uid, pids = user.id, {p1.id: cust1, p2.id: cust2}

    async def fake_fetch(mapping):
        return list(pids[mapping.panel_id])

    with _active_connection(), patch("app.services.oc_user_subscription_hosts._fetch_user_links", new=fake_fetch):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(db, uid, tags, {}, user_tenant_id=None)
    assert len(links) == 8
    bases = {link.split("#", 1)[0] for link in links}
    assert bases == {c.split("#", 1)[0] for c in cust1 + cust2}  # distinct underlying configs, none overwritten
    assert all("discovery" not in link and "d" + "9@" not in link for link in links)


async def test_subscription_is_stable_across_repeated_collects_and_cleanup_cycles():
    disc = [_link(f"d{i}", f"s{i}.example", 443, r) for i, r in enumerate(STYLIZED)]
    cust = [_link(f"c{i}", f"s{i}.example", 443, r) for i, r in enumerate(STYLIZED)]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, disc)
        tags = _tags(panel, rows)
        group = await _group_with_tags(db, tenant, admin, tags)
        user = await _user_with_mapping(db, admin, group, [panel])
        await db.commit()
        uid, gid = user.id, group.id

    async def fake_fetch(_mapping):
        return list(cust)

    seen = []
    for _ in range(3):
        async with GetDB() as db:
            await remove_stale_inbounds(db, CORE_TAGS)  # scheduler cycle
        async with GetDB() as db:
            fresh_tags = sorted(await _membership(db, gid))
        assert fresh_tags == sorted(tags)
        with _active_connection(), patch("app.services.oc_user_subscription_hosts._fetch_user_links", new=fake_fetch):
            async with GetDB() as db:
                seen.append(sorted(await collect_oc_subscription_links_for_user(db, uid, fresh_tags, {}, user_tenant_id=None)))
    assert len(seen[0]) == len(STYLIZED)
    assert seen[0] == seen[1] == seen[2]


async def test_subscription_falls_back_to_remark_matching_when_endpoint_differs():
    """Backward compatible: discovery and customer endpoints may legitimately differ."""
    disc = [_link("d1", "discovery.example", 443, "Tunnel-FR")]
    cust = [_link("c1", "customer.example", 8443, "Tunnel-FR")]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, disc)
        tags = _tags(panel, rows)
        group = await _group_with_tags(db, tenant, admin, tags)
        user = await _user_with_mapping(db, admin, group, [panel])
        await db.commit()
        uid = user.id
    with _active_connection(), patch(
        "app.services.oc_user_subscription_hosts._fetch_user_links", new=AsyncMock(return_value=cust)
    ):
        async with GetDB() as db:
            links = await collect_oc_subscription_links_for_user(db, uid, tags, {}, user_tenant_id=None)
    assert len(links) == 1 and "customer.example" in links[0]


# --------------------------------------------------------------------------------------
# 5. The ~5 minute failure mechanism: membership loss -> reconcile deletes the provisioned user
# --------------------------------------------------------------------------------------


async def _pending_jobs(db, user_id: int, panel_id: int):
    return (
        await db.execute(
            select(OCSyncState).where(
                OCSyncState.entity_id == f"{user_id}_{panel_id}", OCSyncState.status == "pending"
            )
        )
    ).scalars().all()


async def _reconcile_user(db, user_id: int, *, periodic: bool = False):
    user = (
        await db.execute(
            select(User).where(User.id == user_id).options(selectinload(User.groups).selectinload(Group.inbounds))
        )
    ).scalar_one()
    with patch("app.node.oc_sync.tenant_panel_allows_oc_user_mutations", return_value=True):
        await enqueue_oc_user_sync(db, user, periodic_reconcile=periodic)
    await db.commit()


async def test_cleanup_cycle_no_longer_makes_reconcile_delete_the_provisioned_user():
    disc = [_link(f"d{i}", f"k{i}.example", 443, f"Cfg {i}") for i in range(3)]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, disc)
        tags = _tags(panel, rows)
        group = await _group_with_tags(db, tenant, admin, tags)
        user = await _user_with_mapping(db, admin, group, [panel])
        await db.commit()
        uid, pid = user.id, panel.id
        # Desired state == synced state: reconcile is a no-op.
        mapping = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == uid))).scalar_one()
        mapping.last_synced_configs = sorted({(r.source_payload or {}).get("oc_source_config_id") or "" for r in rows} - {""}) or ["c"]
        await db.commit()

    # Fixed behaviour: 10-minute cleanup, then the 5-minute reconcile tick -> nothing to delete.
    async with GetDB() as db:
        await remove_stale_inbounds(db, CORE_TAGS)
    async with GetDB() as db:
        await _reconcile_user(db, uid)
        jobs = await _pending_jobs(db, uid, pid)
        assert not [j for j in jobs if j.operation == "delete"]

    # Old behaviour (unprotected cleanup): membership gone -> reconcile queues DELETE of the user.
    async with GetDB() as db:
        await _old_job_behaviour(db)
    async with GetDB() as db:
        await _reconcile_user(db, uid)
        jobs = await _pending_jobs(db, uid, pid)
        assert [j for j in jobs if j.operation == "delete"], "old cleanup should have triggered a user delete"
        await db.execute(delete(OCSyncState).where(OCSyncState.entity_id == f"{uid}_{pid}"))
        await db.commit()


# --------------------------------------------------------------------------------------
# 6. Issue C: the periodic reconcile tick must never delete a provisioned OC user
# --------------------------------------------------------------------------------------


async def _synced_user(db, tenant, admin, panel, rows, tags):
    group = await _group_with_tags(db, tenant, admin, tags)
    user = await _user_with_mapping(db, admin, group, [panel])
    await db.flush()
    mapping = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == user.id))).scalar_one()
    mapping.last_synced_configs = sorted(
        {(r.source_payload or {}).get("oc_source_config_id") or "" for r in rows} - {""}
    ) or ["c"]
    await db.commit()
    return user.id, group.id


async def _drop_membership(db, group_id: int):
    await db.execute(delete(inbounds_groups_association).where(inbounds_groups_association.c.group_id == group_id))
    await db.commit()


async def test_periodic_reconcile_never_deletes_when_membership_is_missing_but_event_path_still_does():
    disc = [_link(f"p{i}", f"p{i}.example", 443, f"Cfg {i}") for i in range(3)]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, disc)
        uid, gid = await _synced_user(db, tenant, admin, panel, rows, _tags(panel, rows))
        pid = panel.id
        await _drop_membership(db, gid)  # transient inconsistency (cleanup race / incomplete import)

    async with GetDB() as db:
        await _reconcile_user(db, uid, periodic=True)  # the 5-minute tick
        assert not [j for j in await _pending_jobs(db, uid, pid) if j.operation == "delete"]
        mapping = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == uid))).scalar_one()
        assert mapping.status != "deleted" and mapping.last_synced_configs

    # An explicit event (e.g. the group was really removed from the user) still queues the delete.
    async with GetDB() as db:
        await _reconcile_user(db, uid)
        jobs = [j for j in await _pending_jobs(db, uid, pid) if j.operation == "delete"]
        assert len(jobs) == 1 and jobs[0].payload["external_user_id"]
        await db.execute(delete(OCSyncState).where(OCSyncState.entity_id == f"{uid}_{pid}"))
        await db.commit()


async def test_empty_or_incomplete_catalog_never_deletes_the_remote_user_in_either_path():
    disc = [_link(f"e{i}", f"e{i}.example", 443, f"Cfg {i}") for i in range(2)]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, disc)
        uid, _gid = await _synced_user(db, tenant, admin, panel, rows, _tags(panel, rows))
        pid = panel.id
    # Host tags still in the user's groups, but nothing resolves to OC config ids.
    async with GetDB() as db:
        for periodic in (True, False):
            with patch("app.node.oc_sync.resolve_desired_config_ids_for_panel", new=AsyncMock(return_value=[])), patch(
                "app.services.oc_share_link.parse_destination_tag", return_value=None
            ):
                await _reconcile_user(db, uid, periodic=periodic)
            assert not [j for j in await _pending_jobs(db, uid, pid) if j.operation == "delete"]


async def test_periodic_reconcile_job_uses_periodic_mode():
    from app.jobs import reconcile_oc_sync

    seen: list[bool] = []

    async def spy(db, user, *, periodic_reconcile=False):
        seen.append(periodic_reconcile)

    with patch.object(reconcile_oc_sync, "enqueue_oc_user_sync", new=spy):
        await reconcile_oc_sync.reconcile_all_oc_users()
    assert seen, "reconcile job processed no users in the isolated database"
    assert set(seen) == {True}


async def test_reconcile_still_updates_when_desired_configs_change():
    disc = [_link(f"u{i}", f"u{i}.example", 443, f"Cfg {i}") for i in range(3)]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, disc)
        uid, _gid = await _synced_user(db, tenant, admin, panel, rows, _tags(panel, rows))
        pid = panel.id
        mapping = (await db.execute(select(OCUserMapping).where(OCUserMapping.user_id == uid))).scalar_one()
        mapping.last_synced_configs = ["stale-config-id"]
        mapping.status = "active"
        mapping.external_subscription_url = "https://oc.example/sub/x"
        await db.commit()
    async with GetDB() as db:
        await _reconcile_user(db, uid, periodic=True)
        jobs = await _pending_jobs(db, uid, pid)
        assert [j for j in jobs if j.operation in ("update", "create")] and not [j for j in jobs if j.operation == "delete"]
        await db.execute(delete(OCSyncState).where(OCSyncState.entity_id == f"{uid}_{pid}"))
        await db.commit()


# --------------------------------------------------------------------------------------
# 7. Issue D: Unicode-aware remark keys, no silent wrong association
# --------------------------------------------------------------------------------------


def test_normalize_key_is_unicode_aware_and_keeps_distinct_names_distinct():
    from app.services.oc_share_link import _normalize_key

    # stylized math alphabet folds to the plain name, flags/emoji/punctuation/whitespace are ignored
    assert _normalize_key("🇫🇷 𝗙𝗥𝗔𝗡𝗖𝗘 - 𝗠𝗖𝗜") == _normalize_key("france mci") == "francemci"
    assert _normalize_key("  FRANCE   MCI ") == "francemci"
    assert _normalize_key("🇫🇷 𝗙𝗥𝗔𝗡𝗖𝗘 - 𝗠𝗖𝗜") != _normalize_key("🇫🇷 𝗙𝗥𝗔𝗡𝗖𝗘 - 𝗨𝗟𝗧𝗥𝗔")
    # Persian: Arabic yeh/kaf, ZWNJ, tatweel and Persian/Arabic-Indic digits unify; different names stay different
    assert _normalize_key("ايران ۱") == _normalize_key("ایران 1") != ""
    assert _normalize_key("می‌خواهم") == _normalize_key("میخواهم")
    assert _normalize_key("ایران - تهران") != _normalize_key("ایران - شیراز")
    assert _normalize_key("كوتاه") == _normalize_key("کوتاه")
    # nothing meaningful -> empty key, which never matches
    assert _normalize_key("🇫🇷 🔥 ---") == ""
    assert _normalize_key("") == "" and _normalize_key(None) == ""


def test_duplicate_remarks_to_different_endpoints_are_ambiguous_not_first_wins():
    from app.services.oc_share_link import index_subscription_links, match_upstream_link_for_oc_source_config

    a = _link("u1", "a.example", 443, "𝗙𝗥𝗔𝗡𝗖𝗘")
    b = _link("u1", "b.example", 443, "FRANCE")
    c = _link("u1", "c.example", 443, "ایران ۱")
    idx = index_subscription_links([a, b, c])
    assert "france" not in idx  # ambiguous: two endpoints
    assert idx[next(k for k in idx if k != "france")].raw == c
    assert match_upstream_link_for_oc_source_config([a, b], "france") is None
    # same endpoint repeated is not ambiguous
    d = _link("u2", "a.example", 443, "FRANCE")
    assert "france" in index_subscription_links([a, d])


def test_missing_remarks_never_match_each_other_or_a_named_config():
    from app.services.oc_share_link import index_subscription_links, match_upstream_link_for_oc_source_config

    n1 = _link("u1", "n1.example", 443, "")
    n2 = _link("u2", "n2.example", 443, "🔥🔥")
    assert index_subscription_links([n1, n2]) == {}
    assert match_upstream_link_for_oc_source_config([n1, n2], "france") is None


def test_stylized_and_persian_configs_resolve_to_the_right_link():
    from app.services.oc_share_link import match_upstream_link_for_oc_source_config

    links = [_link(f"u{i}", f"h{i}.example", 443, r) for i, r in enumerate(["🇫🇷 𝗙𝗥𝗔𝗡𝗖𝗘 - 𝗠𝗖𝗜", "🇫🇷 𝗙𝗥𝗔𝗡𝗖𝗘 - 𝗨𝗟𝗧𝗥𝗔", "ایران - تهران"])]
    assert match_upstream_link_for_oc_source_config(links, "x1", source_name="France MCI") == links[0]
    assert match_upstream_link_for_oc_source_config(links, "x2", source_name="FRANCE ULTRA") == links[1]
    assert match_upstream_link_for_oc_source_config(links, "x3", source_name="ایران تهران") == links[2]
    assert match_upstream_link_for_oc_source_config(links, "x4", source_name="Germany VIP") is None


async def test_periodic_reconcile_still_deletes_when_loss_is_explicit_disabled_group_or_no_access():
    disc = [_link(f"x{i}", f"x{i}.example", 443, f"Cfg {i}") for i in range(2)]
    async with GetDB() as db:
        tenant, admin = await _tenant_admin(db)
        panel, rows = await _panel(db, tenant, admin, disc)
        uid, gid = await _synced_user(db, tenant, admin, panel, rows, _tags(panel, rows))
        pid = panel.id
        group = await db.get(Group, gid)
        group.is_disabled = True  # explicit operator action, not an inconsistency
        await db.commit()
    async with GetDB() as db:
        await _reconcile_user(db, uid, periodic=True)
        assert [j for j in await _pending_jobs(db, uid, pid) if j.operation == "delete"]
        await db.execute(delete(OCSyncState).where(OCSyncState.entity_id == f"{uid}_{pid}"))
        group = await db.get(Group, gid)
        group.is_disabled = False
        await db.commit()

    # Enabled group + user disabled (no access) is explicit too.
    async with GetDB() as db:
        user = await db.get(User, uid)
        user.status = UserStatus.disabled
        await db.commit()
    async with GetDB() as db:
        await _reconcile_user(db, uid, periodic=True)
        assert [j for j in await _pending_jobs(db, uid, pid) if j.operation == "delete"]
        await db.execute(delete(OCSyncState).where(OCSyncState.entity_id == f"{uid}_{pid}"))
        await db.commit()
