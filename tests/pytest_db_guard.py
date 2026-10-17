"""Fail-closed guards so pytest cannot dispose of or mutate a shared production database."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class DatabaseTarget:
    """Sanitized connection target (no credentials)."""

    driver: str
    host: str | None
    port: int | None
    database: str | None

    def is_sqlite(self) -> bool:
        return self.driver.startswith("sqlite")

    def summary(self) -> str:
        host = self.host or "(none)"
        port = self.port if self.port is not None else "(none)"
        db = self.database or "(none)"
        return f"driver={self.driver} host={host} port={port} database={db}"


def _target_from_url(url: str | Any) -> DatabaseTarget:
    parsed = make_url(str(url))
    driver = parsed.drivername
    host = parsed.host
    port = parsed.port
    database = parsed.database
    return DatabaseTarget(driver=driver, host=host, port=port, database=database)


def app_database_target() -> DatabaseTarget:
    from config import database_settings

    return _target_from_url(database_settings.url)


def session_factory_target() -> DatabaseTarget:
    """Resolved bind for ``SessionLocal`` (may differ from ``engine`` when tests patch the factory)."""
    from app.db.base import SessionLocal, engine

    bind = getattr(SessionLocal, "bind", None) or getattr(SessionLocal, "kw", {}).get("bind") or engine
    sync_engine = getattr(bind, "sync_engine", bind)
    return _target_from_url(sync_engine.url)


def global_engine_target() -> DatabaseTarget:
    from app.db.base import engine

    return _target_from_url(engine.url)


def session_local_is_isolated() -> bool:
    """True when ORM sessions use an in-memory / file SQLite engine, not the app PostgreSQL pool."""
    return session_factory_target().is_sqlite()


def shared_postgres_opt_in() -> bool:
    return os.environ.get("PASARGUARD_PYTEST_ALLOW_APP_POSTGRES", "").strip() == "1"


def oc_cleanup_opt_in() -> bool:
    return os.environ.get("PASARGUARD_PYTEST_OC_CLEANUP", "").strip() == "1"


def oc_cleanup_shared_confirm() -> bool:
    return os.environ.get("PASARGUARD_PYTEST_OC_CLEANUP_CONFIRM_SHARED", "").strip() == "1"


def may_dispose_global_engine() -> bool:
    """True only when the application ``engine`` is private SQLite (safe to dispose in pytest)."""
    return global_engine_target().is_sqlite()


def assert_safe_to_dispose_engine(engine_url: str | Any) -> None:
    """Refuse to dispose a non-SQLite engine (e.g. private fixture disposing app PostgreSQL by mistake)."""
    target = _target_from_url(engine_url)
    if target.is_sqlite():
        return
    raise RuntimeError(
        "Refusing to dispose a non-isolated database engine "
        f"({target.summary()}). Pytest must not close shared PostgreSQL pools."
    )


def assert_safe_for_oc_panel_cleanup() -> None:
    """
    Session teardown cleanup must never run against the live application PostgreSQL DSN.

    SQLite-isolated runs skip cleanup (no shared OC rows). Shared PostgreSQL requires explicit
    double opt-in environment variables.
    """
    if session_local_is_isolated():
        return

    factory = session_factory_target()
    app = app_database_target()

    if factory.is_sqlite():
        return

    if not app.driver.startswith("postgresql"):
        if oc_cleanup_opt_in():
            return
        return

    if oc_cleanup_opt_in() and oc_cleanup_shared_confirm():
        return

    raise RuntimeError(
        "Refusing OC panel session cleanup against a non-isolated database "
        f"(app {app.summary()}, session {factory.summary()}). "
        "Set PASARGUARD_PYTEST_OC_CLEANUP=1 and PASARGUARD_PYTEST_OC_CLEANUP_CONFIRM_SHARED=1 "
        "only when you intend to mutate a shared development database."
    )


def assert_pytest_may_write_database() -> None:
    """Call before tests that insert/update/delete via the default ``SessionLocal``."""
    if session_local_is_isolated() or shared_postgres_opt_in():
        return
    app = app_database_target()
    factory = session_factory_target()
    raise RuntimeError(
        "Refusing database-writing pytest without proven isolation "
        f"(app {app.summary()}, session {factory.summary()}). "
        "Patch SessionLocal to private SQLite (see test_oc_scoped_integration / "
        "test_oc_group_subscription_filter) or set PASARGUARD_PYTEST_ALLOW_APP_POSTGRES=1."
    )
