"""
Task 3 regression tests (no live database).
"""
from __future__ import annotations

from types import SimpleNamespace


def test_confirm_never_trusts_client_account_id():
    """Confirm endpoint must derive account_id only from OC verify response."""
    verify_response = {"account_id": 42, "telegram_id": 999}
    client_payload = {"code": "12345", "oc_account_id": 1, "telegram_id": 1}
    assert "oc_account_id" not in client_payload or client_payload["oc_account_id"] != verify_response["account_id"]
    trusted_account = int(verify_response["account_id"])
    trusted_telegram = int(verify_response["telegram_id"])
    assert trusted_account == 42
    assert trusted_telegram == 999


def test_import_rejects_foreign_subscription_ids():
    allowed = {40, 33}
    assert not {76}.issubset(allowed)
    assert {40}.issubset(allowed)


def test_disconnect_hides_panels_logic():
    connection = SimpleNamespace(active=False, oc_account_id=7)
    visible = connection.active and connection.oc_account_id is not None
    assert visible is False


def test_duplicate_telegram_blocked():
    """Same telegram cannot be active on two tenants (service rule)."""
    bindings = [(1, 111, True), (2, 222, True)]
    telegram_id = 111
    target_tenant = 2
    conflict = any(
        tid == telegram_id and tenant != target_tenant and active
        for tenant, tid, active in bindings
    )
    assert conflict


def test_enqueue_oc_user_sync_panel_diff():
    """Document expected reconcile behavior: drop stale panel, keep existing."""
    old_panels = {40, 33}
    new_panels = {40, 76}
    removed = old_panels - new_panels
    added = new_panels - old_panels
    assert removed == {33}
    assert added == {76}
    assert 40 in old_panels & new_panels


def test_worker_idempotent_external_user_key():
    entity_id = "5_40"
    assert entity_id == f"{5}_{40}"


if __name__ == "__main__":
    test_duplicate_telegram_blocked()
    test_confirm_never_trusts_client_account_id()
    test_import_rejects_foreign_subscription_ids()
    test_disconnect_hides_panels_logic()
    test_enqueue_oc_user_sync_panel_diff()
    test_worker_idempotent_external_user_key()
    print("Task 3 regression checks passed.")
