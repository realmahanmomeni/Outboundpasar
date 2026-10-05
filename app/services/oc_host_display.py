"""OC Host display name vs user subscription template."""

from __future__ import annotations

from app.db.models import ProxyHost
from app.db.models_oc import OCPanelConfig

DEFAULT_OC_HOST_DISPLAY_TEMPLATE = (
    "📡Status|{STATUS_EMOJI}|📆({DAYS_LEFT} day)|({DATA_USAGE}-{DATA_LIMIT})📊\n"
    "👤v1.7|{USERNAME}"
)

_TEMPLATE_MARKERS = ("{USERNAME}", "{STATUS_EMOJI}", "{DAYS_LEFT}", "{DATA_USAGE}", "{DATA_LIMIT}")


def remark_looks_like_user_template(remark: str | None) -> bool:
    if not remark:
        return False
    text = remark.strip()
    if not text:
        return False
    if text == DEFAULT_OC_HOST_DISPLAY_TEMPLATE.strip():
        return True
    return any(marker in text for marker in _TEMPLATE_MARKERS)


def resolve_oc_host_display_name(host: ProxyHost, config: OCPanelConfig) -> str:
    """Operator-facing Host name (not the per-user subscription template)."""
    remark = (host.remark or "").strip()
    if remark and not remark_looks_like_user_template(remark):
        return remark
    return (config.source_name or remark or config.source_config_id or "").strip()


def resolve_oc_subscription_remark_source(
    *,
    display_name: str | None,
    display_name_template: str | None,
    fallback: str = "",
) -> str:
    """
    Choose the string to pass through ``format_map`` for a subscription config remark.

    Auto-imported destination hosts get ``DEFAULT_OC_HOST_DISPLAY_TEMPLATE`` on
    ``display_name_template``; operator-facing names live in ``display_name``. Use the
    template only when the operator customized it away from the default presentation stub.
    """
    custom = (display_name_template or "").strip()
    display = (display_name or "").strip()
    if custom and custom != DEFAULT_OC_HOST_DISPLAY_TEMPLATE.strip():
        return custom
    if display and not remark_looks_like_user_template(display):
        return display
    return custom or display or fallback


def repair_oc_host_remark_if_template(host: ProxyHost, config: OCPanelConfig) -> bool:
    """
    If remark was incorrectly set to the user template, restore source config name.
    Returns True when remark was changed (caller may flush/commit).
    """
    if not remark_looks_like_user_template(host.remark):
        return False
    desired = (config.source_name or config.source_config_id or "").strip()
    if not desired:
        return False
    host.remark = desired
    return True
