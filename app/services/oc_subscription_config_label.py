"""Destination subscription config names for OC Group picker labels."""

from __future__ import annotations

from app.db.models_oc import OCPanelConfig
from app.services.oc_share_link import parse_share_link


def destination_subscription_config_name(config: OCPanelConfig) -> str | None:
    """
    Human-facing name from the discovery/test user's destination subscription link.

    Sourced from materialized ``source_payload`` (refresh_panel_hosts_from_discovery_subscription).
    Does not call live URLs and does not use customer OCUserMapping subscriptions.
    """
    payload = config.source_payload if isinstance(config.source_payload, dict) else {}
    if not str(payload.get("subscription_link") or "").strip():
        return None

    parsed = payload.get("subscription_parsed")
    if isinstance(parsed, dict):
        remark = str(parsed.get("remark") or "").strip()
        if remark:
            return remark

    link = str(payload.get("subscription_link") or "").strip()
    if link:
        share = parse_share_link(link)
        if share and (share.remark or "").strip():
            return share.remark.strip()

    return None
