"""The usage job must not flood ERROR logs for an integration whose token can never be decrypted."""

from __future__ import annotations

import logging

import pytest

from app.node import oc_usage


@pytest.fixture(autouse=True)
def _reset_state():
    oc_usage._decrypt_failure_last_logged.clear()
    yield
    oc_usage._decrypt_failure_last_logged.clear()


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def test_first_failure_is_error_and_repeats_inside_window_are_not(caplog):
    caplog.set_level(logging.DEBUG, logger="oc-usage")
    logged = [oc_usage._log_decrypt_failure(7, ValueError("bad"), now=1000.0 + i * 10) for i in range(50)]
    assert logged[0] is True
    assert not any(logged[1:]), "repeats within the window must be demoted"
    assert len(_errors(caplog)) == 1


def test_error_is_emitted_again_after_the_window(caplog):
    caplog.set_level(logging.DEBUG, logger="oc-usage")
    assert oc_usage._log_decrypt_failure(7, ValueError("bad"), now=0.0)
    assert not oc_usage._log_decrypt_failure(7, ValueError("bad"), now=oc_usage.DECRYPT_FAILURE_LOG_INTERVAL_S - 1)
    assert oc_usage._log_decrypt_failure(7, ValueError("bad"), now=oc_usage.DECRYPT_FAILURE_LOG_INTERVAL_S + 1)
    assert len(_errors(caplog)) == 2


def test_each_integration_is_rate_limited_independently(caplog):
    caplog.set_level(logging.DEBUG, logger="oc-usage")
    assert oc_usage._log_decrypt_failure(1, ValueError("x"), now=0.0)
    assert oc_usage._log_decrypt_failure(2, ValueError("x"), now=1.0), "a different integration still logs"
    assert not oc_usage._log_decrypt_failure(1, ValueError("x"), now=2.0)
    assert len(_errors(caplog)) == 2


def test_a_real_failure_is_never_silenced_entirely(caplog):
    caplog.set_level(logging.DEBUG, logger="oc-usage")
    oc_usage._log_decrypt_failure(9, ValueError("boom"), now=0.0)
    messages = _errors(caplog)
    assert messages and "integration 9" in messages[0]
