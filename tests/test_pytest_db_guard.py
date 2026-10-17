"""Regression tests for pytest database isolation guards."""

from __future__ import annotations

import pytest

from tests.pytest_db_guard import (
    assert_safe_for_oc_panel_cleanup,
    assert_safe_to_dispose_engine,
    global_engine_target,
    may_dispose_global_engine,
    session_local_is_isolated,
)


def test_global_engine_target_has_no_credentials():
    target = global_engine_target()
    assert target.driver
    assert target.summary()
    assert "@" not in target.summary()


def test_global_engine_dispose_disabled_for_postgresql():
    if global_engine_target().is_sqlite():
        pytest.skip("app engine is sqlite in this environment")
    assert may_dispose_global_engine() is False


def test_dispose_guard_refuses_postgresql_engine_url():
    with pytest.raises(RuntimeError, match="Refusing to dispose"):
        assert_safe_to_dispose_engine("postgresql+asyncpg://user@127.0.0.1:5432/example")


def test_cleanup_guard_refuses_shared_postgres_without_opt_in(monkeypatch):
    monkeypatch.delenv("PASARGUARD_PYTEST_OC_CLEANUP", raising=False)
    monkeypatch.delenv("PASARGUARD_PYTEST_OC_CLEANUP_CONFIRM_SHARED", raising=False)
    if session_local_is_isolated():
        pytest.skip("session factory already isolated")
    if not global_engine_target().driver.startswith("postgresql"):
        pytest.skip("not using postgresql app DSN")
    with pytest.raises(RuntimeError, match="Refusing OC panel session cleanup"):
        assert_safe_for_oc_panel_cleanup()
