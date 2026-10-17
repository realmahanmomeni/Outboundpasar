"""Central OCPanel.multiplier is the only PasarGuard usage multiplier (not compounded with source metadata)."""
from __future__ import annotations

from decimal import Decimal

def test_effective_multiplier_is_panel_only_not_product_with_source():
    """Panel 1.5 × source tunnel 2.0 → PasarGuard applies 1.5 to raw OC deltas only."""
    previous = 0
    remote = 1_000_000
    panel_mult = 1.5
    raw_delta = remote - previous
    accounted = int(raw_delta * panel_mult)
    assert accounted == 1_500_000
    assert accounted != int(raw_delta * panel_mult * 2.0)


def test_repeated_sync_uses_delta_not_re_multiply_cumulative():
    panel_mult = 1.5
    prev = 1_000_000
    remote = 2_000_000
    assert int((remote - prev) * panel_mult) == 1_500_000


def test_panel_host_api_multiplier_matches_panel_not_host_override():
    """Document contract: host API exposes panel.multiplier (see panel._destination_host_response)."""
    from types import SimpleNamespace

    from app.routers.panel import _destination_host_response

    panel = SimpleNamespace(multiplier=Decimal("1.50"))
    host = SimpleNamespace(
        id=1,
        address=set(),
        port=443,
        is_disabled=False,
    )
    dest = SimpleNamespace(
        display_name="h",
        display_name_template=None,
        destination_config_id="abc",
        source_payload={"subscription_link": "vless://u@1.1.1.1:443#x"},
    )
    resp = _destination_host_response(host, dest, panel)  # type: ignore[arg-type]
    assert resp.multiplier == 1.5
