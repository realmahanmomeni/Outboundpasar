"""Focused OC usage delta × panel multiplier math (isolated, no DB)."""


def test_accounted_delta_first_sync():
    previous = 0
    current = 2_000_000
    multiplier = 4.0
    raw_delta = current - previous
    assert int(raw_delta * multiplier) == 8_000_000


def test_accounted_delta_second_sync_no_double_count():
    previous = 2_000_000
    current = 3_000_000
    multiplier = 4.0
    raw_delta = current - previous
    assert int(raw_delta * multiplier) == 4_000_000
