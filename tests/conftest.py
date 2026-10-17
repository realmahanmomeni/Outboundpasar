"""Shared DB hygiene: drop test/mock OC panels after the pytest session."""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_oc_panels_after_session():
    yield
    from tests.pytest_db_guard import assert_safe_for_oc_panel_cleanup, session_local_is_isolated

    try:
        assert_safe_for_oc_panel_cleanup()
    except RuntimeError:
        return
    if session_local_is_isolated():
        return
    from scripts.cleanup_test_oc_panels import run

    asyncio.run(run(apply=True))
