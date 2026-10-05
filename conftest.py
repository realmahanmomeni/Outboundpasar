"""Pytest configuration for PasarGuard integration tests."""

pytest_plugins = ("pytest_asyncio",)

import pytest


@pytest.fixture(autouse=True)
async def _dispose_sqlalchemy_engine_after_async_test():
    """Drop pooled asyncpg connections so the next test's event loop does not reuse them."""
    yield
    from app.db.base import engine

    await engine.dispose()
