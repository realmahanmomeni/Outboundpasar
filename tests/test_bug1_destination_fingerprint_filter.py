"""BUG 1: destination-fingerprint sync filtering (user 7713 class of leaks)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.workspace import create_workspace_for_administrator
from sqlalchemy import func

from app.db.models import Admin, AdminRole, Tenant, TenantStatus, users_groups_association
from app.db.models_oc import OCIntegration, OCPanel
from app.db.crud.admin import build_admin_details
from app.services.oc_destination_runtime import destination_config_allowed_by_sync
from app.services.workspace_scope import ensure_actor_panel_access


# --- destination_config_allowed_by_sync (cases 1–5) ---

SELECTED_FP = "ac2d9f8e575107bc"
OTHER_FP = "bbbbbbbbbbbbbbbb"


def test_case1_selected_destination_without_catalog_id_allowed():
    assert destination_config_allowed_by_sync(
        oc_source_config_id="",
        panel_id=6454,
        destination_config_id=SELECTED_FP,
        synced={SELECTED_FP},
    )


def test_case2_unselected_destination_denied_when_sync_restricted():
    assert not destination_config_allowed_by_sync(
        oc_source_config_id="",
        panel_id=6454,
        destination_config_id=OTHER_FP,
        synced={SELECTED_FP},
    )


def test_case3_empty_last_synced_denies_destination_only_hosts():
    assert not destination_config_allowed_by_sync(
        oc_source_config_id="",
        panel_id=6454,
        destination_config_id=SELECTED_FP,
        synced=set(),
    )


def test_case4_missing_dest_and_catalog_identity_denied():
    assert not destination_config_allowed_by_sync(
        oc_source_config_id="",
        panel_id=1,
        destination_config_id="",
        synced={SELECTED_FP},
    )
    assert not destination_config_allowed_by_sync(
        oc_source_config_id="orphan-catalog",
        panel_id=1,
        destination_config_id="",
        synced={SELECTED_FP},
    )


def test_case5_full_group_all_fingerprints_in_sync_allowed():
    full_sync = {SELECTED_FP, OTHER_FP, "cccccccccccccccc"}
    for fp in (SELECTED_FP, OTHER_FP):
        assert destination_config_allowed_by_sync(
            oc_source_config_id="",
            panel_id=6454,
            destination_config_id=fp,
            synced=full_sync,
        )


def test_subscription_upstream_fetch_excludes_unselected_destination_fingerprint():
    from app.services.oc_user_subscription_hosts import OcSubscriptionLinksState, _panel_ids_for_upstream_fetch

    selected_tag = f"oc_6454_d_{SELECTED_FP}"
    dest_selected = SimpleNamespace(
        panel_id=6454,
        destination_config_id=SELECTED_FP,
        source_payload={},
    )
    dest_other = SimpleNamespace(
        panel_id=6454,
        destination_config_id=OTHER_FP,
        source_payload={},
    )
    mapping = SimpleNamespace(user_id=7713, panel_id=6454, external_subscription_url="https://example.com/sub")
    state = OcSubscriptionLinksState(
        user_id=7713,
        oc_tags=[selected_tag],
        allowed_panels={6454},
        synced_by_panel={6454: {SELECTED_FP}},
        mappings={6454: mapping},
        dest_tags=[selected_tag],
        legacy_tags=[],
        catalog_names={},
        dest_rows=[(dest_selected, None), (dest_other, None)],
        legacy_config_rows=[],
    )
    panel_ids = _panel_ids_for_upstream_fetch(state)
    assert panel_ids == {6454}


@pytest.mark.asyncio
async def test_case6_sync_keys_match_selected_destination_tag():
    from app.node.oc_sync import resolve_desired_sync_keys_for_panel

    tag = f"oc_6454_d_{SELECTED_FP}"
    with patch(
        "app.node.oc_sync.resolve_desired_config_ids_for_panel",
        new=AsyncMock(return_value=[]),
    ):
        keys = await resolve_desired_sync_keys_for_panel(AsyncMock(), 6454, [tag])
    assert keys == [SELECTED_FP]


@pytest.mark.asyncio
async def test_enqueue_oc_user_sync_does_not_change_user_group_membership():
    from app.node.oc_sync import enqueue_oc_user_sync
    from app.db.models import Group, ProxyInbound, User, UserStatus
    from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts
    from app.services.oc_share_link import destination_virtual_inbound_tag

    async with GetDB() as db:
        tenant = Tenant(name=f"t_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin = Admin(
            username=f"adm_{uuid.uuid4().hex[:8]}",
            hashed_password="x",
            role_id=role.id,
            tenant_id=tenant.id,
        )
        db.add(admin)
        await db.flush()
        await create_workspace_for_administrator(db, admin)
        ws = admin.workspace_id
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="1513",
            purchaser_identity="buyer",
            name="Panel",
            tenant_id=tenant.id,
            workspace_id=ws,
            sync_status="connected",
        )
        db.add(panel)
        await db.flush()
        await reconcile_panel_destination_hosts(
            db, panel, ["vless://u@1.1.1.1:443?encryption=none#host"]
        )
        await db.flush()
        from app.db.models_oc import OCPanelDestinationHost

        dest = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalar_one()
        tag = destination_virtual_inbound_tag(panel.id, dest.destination_config_id)
        inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one()
        group = Group(name="test", inbounds=[inbound], tenant_id=tenant.id, workspace_id=ws)
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=ws,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        async def _group_count(uid: int) -> int:
            return int(
                await db.scalar(
                    select(func.count())
                    .select_from(users_groups_association)
                    .where(users_groups_association.c.user_id == uid)
                )
                or 0
            )

        count_before = await _group_count(user.id)

        with patch(
            "app.node.oc_sync.tenant_panel_allows_oc_user_mutations",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await enqueue_oc_user_sync(db, user)
        count_after = await _group_count(user.id)
        assert count_before == count_after
        assert count_before == 1
        await db.rollback()


@pytest.mark.asyncio
async def test_periodic_reconcile_skips_mapping_with_empty_last_synced_configs():
    from app.node.oc_sync import enqueue_oc_user_sync
    from app.db.models import Group, ProxyInbound, User, UserStatus
    from app.db.models_oc import OCUserMapping, OCSyncState
    from app.services.oc_share_link import destination_virtual_inbound_tag
    from app.services.oc_panel_destination_hosts import reconcile_panel_destination_hosts

    async with GetDB() as db:
        tenant = Tenant(name=f"t_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin = Admin(
            username=f"adm_{uuid.uuid4().hex[:8]}",
            hashed_password="x",
            role_id=role.id,
            tenant_id=tenant.id,
        )
        db.add(admin)
        await db.flush()
        await create_workspace_for_administrator(db, admin)
        ws = admin.workspace_id
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="1513",
            purchaser_identity="buyer",
            name="Panel",
            tenant_id=tenant.id,
            workspace_id=ws,
            sync_status="connected",
        )
        db.add(panel)
        await db.flush()
        await reconcile_panel_destination_hosts(
            db, panel, ["vless://u@1.1.1.1:443?encryption=none#host"]
        )
        await db.flush()
        from app.db.models_oc import OCPanelDestinationHost

        dest = (
            await db.execute(select(OCPanelDestinationHost).where(OCPanelDestinationHost.panel_id == panel.id))
        ).scalar_one()
        tag = destination_virtual_inbound_tag(panel.id, dest.destination_config_id)
        inbound = (await db.execute(select(ProxyInbound).where(ProxyInbound.tag == tag))).scalar_one()
        group = Group(name="test", inbounds=[inbound], tenant_id=tenant.id, workspace_id=ws)
        db.add(group)
        await db.flush()
        user = User(
            username=f"u_{uuid.uuid4().hex[:6]}",
            status=UserStatus.active,
            admin_id=admin.id,
            workspace_id=ws,
            proxy_settings={"vless": {"id": str(uuid.uuid4())}},
        )
        user.groups = [group]
        db.add(user)
        await db.flush()
        mapping = OCUserMapping(
            user_id=user.id,
            panel_id=panel.id,
            external_user_id=str(uuid.uuid4()),
            status="active",
            last_synced_configs=[],
        )
        db.add(mapping)
        await db.flush()

        with patch(
            "app.node.oc_sync.tenant_panel_allows_oc_user_mutations",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await enqueue_oc_user_sync(db, user, periodic_reconcile=True)
        await db.flush()
        jobs = (
            await db.execute(
                select(OCSyncState).where(
                    OCSyncState.entity_type == "user_mapping",
                    OCSyncState.entity_id == f"{user.id}_{panel.id}",
                    OCSyncState.status == "pending",
                )
            )
        ).scalars().all()
        assert jobs == []
        await db.rollback()


@pytest.mark.asyncio
async def test_sync_panel_from_oc_preserves_existing_group_is_selected():
    from app.node.oc_sync import sync_panel_from_outbound_center
    from app.db.models_oc import OCPanelGroup

    async with GetDB() as db:
        tenant = Tenant(name=f"t_{uuid.uuid4().hex[:8]}", status=TenantStatus.active)
        db.add(tenant)
        await db.flush()
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel = OCPanel(
            integration_id=intg.id,
            source_panel_id="99",
            purchaser_identity="1",
            name="P",
            tenant_id=tenant.id,
            sync_status="connected",
        )
        db.add(panel)
        await db.flush()
        og = OCPanelGroup(
            panel_id=panel.id,
            source_group_id="1",
            source_name="Old",
            is_selected=True,
        )
        db.add(og)
        await db.flush()

        async def fake_oc_api(_integration, method, path, **kwargs):
            if path.endswith("/groups"):
                return {"groups": [{"id": "1", "name": "Renamed"}]}
            if path.endswith("/configs"):
                return {"configs": []}
            return {"status": "active"}

        with patch(
            "app.services.oc_connection_credentials.connection_token_for_panel",
            new=AsyncMock(return_value="tok"),
        ), patch(
            "app.services.oc_integration_client.call_oc_api",
            new=AsyncMock(side_effect=fake_oc_api),
        ):
            await sync_panel_from_outbound_center(db, panel.id)
        await db.refresh(og)
        assert og.is_selected is True
        assert og.source_name == "Renamed"
        await db.rollback()


@pytest.mark.asyncio
async def test_buyer_test_user_cross_workspace_panel_denied():
    async with GetDB() as db:
        tenant_a = Tenant(name=f"a_{uuid.uuid4().hex[:6]}", status=TenantStatus.active)
        tenant_b = Tenant(name=f"b_{uuid.uuid4().hex[:6]}", status=TenantStatus.active)
        db.add_all([tenant_a, tenant_b])
        await db.flush()
        role = (await db.execute(select(AdminRole).where(AdminRole.name == "administrator"))).scalar_one()
        admin_a = Admin(
            username=f"adm_a_{uuid.uuid4().hex[:6]}",
            hashed_password="x",
            role_id=role.id,
            tenant_id=tenant_a.id,
        )
        admin_b = Admin(
            username=f"adm_b_{uuid.uuid4().hex[:6]}",
            hashed_password="x",
            role_id=role.id,
            tenant_id=tenant_b.id,
        )
        db.add_all([admin_a, admin_b])
        await db.flush()
        await create_workspace_for_administrator(db, admin_a)
        await create_workspace_for_administrator(db, admin_b)
        intg = OCIntegration(base_url="https://oc.example", api_token_encrypted="e", token_preview="t")
        db.add(intg)
        await db.flush()
        panel_b = OCPanel(
            integration_id=intg.id,
            source_panel_id="1711",
            purchaser_identity="x",
            name="B panel",
            tenant_id=tenant_b.id,
            workspace_id=admin_b.workspace_id,
            panel_type="REBECCA",
        )
        db.add(panel_b)
        await db.flush()
        details_a = build_admin_details(admin_a, include_loaded_metrics=False)
        with pytest.raises(HTTPException) as exc:
            await ensure_actor_panel_access(db, panel_b, is_owner=False, admin=details_a)
        assert exc.value.status_code == 404
        await db.rollback()
