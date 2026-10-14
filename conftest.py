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
async def _dispose_sqlalchemy_engine_after_async_test():
    """Drop pooled asyncpg connections so the next test's event loop does not reuse them."""
    yield
    from app.db.base import engine

    await engine.dispose()
