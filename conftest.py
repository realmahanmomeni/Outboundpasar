"""Pytest configuration for PasarGuard integration tests."""

pytest_plugins = ("pytest_asyncio",)

import pytest


@pytest.fixture(autouse=True)
def _stub_custom_domain_certificate_provisioner(monkeypatch):
    from app.services.custom_domain.ssl_lifecycle import StubCertificateProvisioner, set_certificate_provisioner
    from config import custom_domain_settings

    set_certificate_provisioner(StubCertificateProvisioner())
    monkeypatch.setattr(custom_domain_settings, "ingress_public_https_ready", True)
    yield


@pytest.fixture(autouse=True)
def _reset_subscription_process_caches():
    """Process-global subscription caches must never leak state between tests."""
    from app.services.oc_upstream_links_cache import clear_upstream_links_cache
    from app.subscription.config_cache import clear_sub_config_cache

    clear_sub_config_cache()
    clear_upstream_links_cache()
    yield
    clear_sub_config_cache()
    clear_upstream_links_cache()


@pytest.fixture(autouse=True)
async def _dispose_sqlalchemy_engine_after_async_test():
    """Drop pooled connections for in-memory SQLite tests only.

    Disposing the shared PostgreSQL engine after every async test breaks pooled
    connections (asyncpg ``ConnectionDoesNotExistError``) for later tests and can
    disturb a concurrently running panel process using the same DSN.
    """
    yield
    from tests.pytest_db_guard import may_dispose_global_engine

    if not may_dispose_global_engine():
        return
    from app.db.base import engine

    await engine.dispose()
