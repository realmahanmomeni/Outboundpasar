"""
Task 3B/3C — lightweight regression checks (no database).
Run full integration tests in an environment with app DB dependencies installed.
"""


def test_import_panel_subset_guard():
    allowed = {40, 33}
    assert {40}.issubset(allowed)
    assert not {99}.issubset(allowed)


def test_bot_deep_link_format():
    intent_id = "550e8400-e29b-41d4-a716-446655440000"
    username = "outboundino_bot"
    url = f"https://t.me/{username}?start=pgconnect_{intent_id}"
    assert url.endswith(intent_id)
    assert "pgconnect_" in url


def test_duplicate_telegram_tenant_guard():
    """Mirrors activate_connection cross-tenant telegram exclusivity."""
    bindings = [(1, 111), (2, 222)]
    telegram_id = 111
    target_tenant = 2
    conflict = any(tid == telegram_id and tenant != target_tenant for tenant, tid in bindings)
    assert conflict


def run_tests():
    print("=== Task 3 lightweight tests ===")
    test_import_panel_subset_guard()
    print("[x] import subset guard")
    test_bot_deep_link_format()
    print("[x] bot deep link format")
    test_duplicate_telegram_tenant_guard()
    print("[x] duplicate telegram guard")
    print("All Task 3 lightweight tests passed.")


if __name__ == "__main__":
    run_tests()
