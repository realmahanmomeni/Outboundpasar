"""Reusable OC panel configuration pipeline audit (upstream → subscription)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Group, ProxyInbound
from app.db.models_oc import OCPanel, OCPanelConfig, OCPanelDestinationHost
from app.services.assignable_hosts import list_group_host_options
from app.services.oc_connection_credentials import call_oc_panel_api
from app.services.oc_integration_client import OcIntegrationApiError
from app.services.oc_share_link import (
    decode_subscription_links,
    is_importable_configuration_link,
    parse_share_link,
    subscription_config_links,
    subscription_link_uri_fingerprint,
)


@dataclass
class LineClassification:
    line_number: int
    raw_preview: str
    category: str
    detail: str = ""


@dataclass
class PanelConfigAuditReport:
    panel_id: int
    source_panel_id: str
    panel_name: str
    upstream_line_count: int = 0
    valid_supported_uri_count: int = 0
    unsupported_scheme_count: int = 0
    malformed_count: int = 0
    metadata_count: int = 0
    duplicate_fingerprint_count: int = 0
    decoded_importable_count: int = 0
    destination_link_count: int = 0
    catalog_config_count: int = 0
    destination_host_visible_count: int = 0
    runtime_inbound_count: int = 0
    group_selectable_count: int = 0
    line_classifications: list[LineClassification] = field(default_factory=list)
    fetch_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "panel_id": self.panel_id,
            "source_panel_id": self.source_panel_id,
            "panel_name": self.panel_name,
            "upstream_subscription_lines": self.upstream_line_count,
            "valid_supported_uris": self.valid_supported_uri_count,
            "unsupported_schemes": self.unsupported_scheme_count,
            "malformed": self.malformed_count,
            "metadata_or_other": self.metadata_count,
            "duplicate_fingerprints": self.duplicate_fingerprint_count,
            "decoded_importable": self.decoded_importable_count,
            "destination_subscription_links": self.destination_link_count,
            "catalog_configs": self.catalog_config_count,
            "destination_hosts_visible": self.destination_host_visible_count,
            "runtime_inbounds": self.runtime_inbound_count,
            "group_selectable": self.group_selectable_count,
            "fetch_error": self.fetch_error,
            "lines": [
                {
                    "n": row.line_number,
                    "category": row.category,
                    "detail": row.detail,
                    "preview": row.raw_preview,
                }
                for row in self.line_classifications
            ],
        }


_SUPPORTED_PREFIXES = (
    "vless://",
    "vmess://",
    "trojan://",
    "ss://",
    "hysteria2://",
    "hy2://",
    "wireguard://",
)


def _classify_upstream_line(line: str) -> tuple[str, str]:
    raw = (line or "").strip()
    if not raw:
        return "empty", "blank line"
    lower = raw.lower()
    if lower.startswith("<") or "<html" in lower or "<!doctype" in lower:
        return "metadata", "html fragment"
    if any(lower.startswith(prefix) for prefix in _SUPPORTED_PREFIXES):
        if is_importable_configuration_link(raw):
            return "valid_supported_uri", ""
        if parse_share_link(raw) is not None:
            return "malformed", "recognized scheme but failed importability check"
        return "malformed", "scheme prefix present but parse failed"
    if "://" in raw:
        scheme = raw.split("://", 1)[0].lower()
        return "unsupported_scheme", scheme
    return "metadata", "non-uri text"


def classify_upstream_subscription_body(body: str) -> tuple[list[LineClassification], dict[str, int]]:
    """Classify each non-empty line in a plaintext subscription body."""
    rows: list[LineClassification] = []
    counts = {
        "valid_supported_uri": 0,
        "unsupported_scheme": 0,
        "malformed": 0,
        "metadata": 0,
        "empty": 0,
    }
    for idx, line in enumerate(body.replace("\r\n", "\n").split("\n"), start=1):
        category, detail = _classify_upstream_line(line)
        if category == "empty":
            counts["empty"] += 1
            continue
        if category in counts:
            counts[category] += 1
        preview = line.strip()
        if len(preview) > 120:
            preview = preview[:117] + "..."
        rows.append(LineClassification(line_number=idx, raw_preview=preview, category=category, detail=detail))
    return rows, counts


async def fetch_discovery_subscription_body(db: AsyncSession, panel: OCPanel) -> str | None:
    test_user_id = (panel.test_user_id or "").strip()
    if not test_user_id:
        return None
    path = f"/v1/integration/panels/{panel.source_panel_id}/users/{test_user_id}"
    try:
        user_data = await call_oc_panel_api(db, panel, "GET", path, None)
    except OcIntegrationApiError:
        return None
    from app.services.oc_upstream_subscription import fetch_upstream_subscription_body

    subscription_url = str((user_data or {}).get("subscription_url") or "").strip()
    if not subscription_url:
        return None
    raw = await fetch_upstream_subscription_body(subscription_url, "links")
    return raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")


async def audit_panel_configuration_pipeline(
    db: AsyncSession,
    panel: OCPanel,
    *,
    subscription_body: str | None = None,
) -> PanelConfigAuditReport:
    panel = (
        await db.execute(
            select(OCPanel).options(selectinload(OCPanel.integration)).where(OCPanel.id == panel.id)
        )
    ).scalar_one()

    report = PanelConfigAuditReport(
        panel_id=panel.id,
        source_panel_id=str(panel.source_panel_id),
        panel_name=panel.name or "",
    )

    body = subscription_body
    if body is None:
        try:
            body = await fetch_discovery_subscription_body(db, panel)
        except Exception as exc:
            report.fetch_error = f"{type(exc).__name__}: {exc}"
            body = None

    if body:
        rows, counts = classify_upstream_subscription_body(body)
        report.line_classifications = rows
        report.upstream_line_count = len(rows)
        report.valid_supported_uri_count = counts["valid_supported_uri"]
        report.unsupported_scheme_count = counts["unsupported_scheme"]
        report.malformed_count = counts["malformed"]
        report.metadata_count = counts["metadata"]
        decoded = decode_subscription_links(body)
        report.decoded_importable_count = len(decoded)
        dest_links = subscription_config_links(decoded)
        report.destination_link_count = len(dest_links)
        seen_fp: set[str] = set()
        for link in decoded:
            fp = subscription_link_uri_fingerprint(link)
            if fp in seen_fp:
                report.duplicate_fingerprint_count += 1
            seen_fp.add(fp)

    report.catalog_config_count = int(
        await db.scalar(
            select(func.count())
            .select_from(OCPanelConfig)
            .where(OCPanelConfig.panel_id == panel.id, OCPanelConfig.source_missing.is_(False))
        )
        or 0
    )
    report.destination_host_visible_count = int(
        await db.scalar(
            select(func.count())
            .select_from(OCPanelDestinationHost)
            .where(
                OCPanelDestinationHost.panel_id == panel.id,
                OCPanelDestinationHost.source_missing.is_(False),
                OCPanelDestinationHost.locally_hidden.is_(False),
            )
        )
        or 0
    )
    report.runtime_inbound_count = report.destination_host_visible_count
    opts = await list_group_host_options(
        db, tenant_id=panel.tenant_id, workspace_id=panel.workspace_id
    )
    report.group_selectable_count = len([o for o in opts if o.panel_id == panel.id])
    return report


async def audit_group_subscription_counts(
    db: AsyncSession,
    group: Group,
    user_id: int | None = None,
) -> dict[str, int]:
    """Stage counts for a Group → user subscription path (read-only)."""
    await group.awaitable_attrs.inbounds
    selected = len(group.inbounds)
    dest_tags = [i.tag for i in group.inbounds if (i.tag or "").startswith("oc_")]
    counts: dict[str, int] = {
        "group_selected_inbounds": selected,
        "group_selected_oc_destination_tags": len(dest_tags),
    }
    if user_id is not None:
        from app.services.oc_subscription_runtime import (
            filter_oc_inbound_tags_for_runtime_subscription,
            resolve_user_tenant_id,
        )

        tenant_id = await resolve_user_tenant_id(db, user_id, None)
        runtime = await filter_oc_inbound_tags_for_runtime_subscription(
            db, user_id, dest_tags, tenant_id
        )
        counts["runtime_oc_tags_after_filter"] = len(runtime)
    return counts
