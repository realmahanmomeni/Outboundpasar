"""Shared DB hygiene: drop test/mock OC panels after the pytest session."""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_oc_panels_after_session():
    yield
    from scripts.cleanup_test_oc_panels import run

    asyncio.run(run(apply=True))
