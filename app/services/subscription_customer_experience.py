"""Phase 3-E: customer subscription page experience (templates + branding, safe fallbacks)."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.subscription_branding import resolve_subscription_page_experience
from app.services.subscription_experience import resolve_subscription_experience_context
from app.templates import render_template
from config import template_settings

_UNSAFE_CSS = re.compile(r"</style|<\s*script", re.IGNORECASE)
_MAX_CUSTOM_CSS_LEN = 16_384


def _sanitize_http_url(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if not text.lower().startswith(("http://", "https://")) or len(text) > 2048:
        return None
    return text


def sanitize_experience_for_render(experience: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop unsafe customization; never raise."""
    if not experience:
        return None
    try:
        cleaned = dict(experience)
        favicon = cleaned.get("favicon_url")
        if favicon is not None:
            cleaned["favicon_url"] = _sanitize_http_url(str(favicon))

        css = cleaned.get("custom_css")
        if css is not None:
            css_text = str(css)
            if _UNSAFE_CSS.search(css_text) or len(css_text) > _MAX_CUSTOM_CSS_LEN:
                cleaned.pop("custom_css", None)
            else:
                cleaned["custom_css"] = css_text

        hide = cleaned.get("hide_platform_branding")
        cleaned["hide_platform_branding"] = bool(hide)

        for key in ("title", "description", "footer_text", "customer_footer", "support_text", "logo_url"):
            val = cleaned.get(key)
            if val is not None and not str(val).strip():
                cleaned.pop(key, None)

        if cleaned.get("logo_url"):
            cleaned["logo_url"] = _sanitize_http_url(str(cleaned["logo_url"])) or cleaned.get("logo_url")

        return cleaned
    except Exception:
        return None


async def resolve_customer_subscription_experience(
    db: AsyncSession,
    *,
    workspace_id: int | None,
) -> dict[str, Any] | None:
    """Resolve merged template + branding for HTML subscription pages. Fail closed to default page."""
    try:
        template_ctx = await resolve_subscription_experience_context(db, workspace_id=workspace_id)
        merged = await resolve_subscription_page_experience(
            db,
            workspace_id=workspace_id,
            experience_from_template=template_ctx,
        )
        return sanitize_experience_for_render(merged)
    except Exception:
        return None


def subscription_preview_body_payload(experience: dict[str, Any] | None) -> dict[str, Any]:
    """Minimal payload for admin preview of the real subscription page template."""
    from types import SimpleNamespace

    from app.db.models import DataLimitResetStrategy, UserStatus

    user = SimpleNamespace(
        username="preview-user",
        status=SimpleNamespace(value=UserStatus.active.value),
        data_limit=None,
        used_traffic=0,
        data_limit_reset_strategy=SimpleNamespace(value=DataLimitResetStrategy.no_reset.value),
        expire=None,
        on_hold_expire_duration=None,
        on_hold_timeout=None,
    )
    hide_apps = bool((experience or {}).get("hide_platform_branding"))
    return {
        "user": user,
        "links": ["vless://preview-example"],
        "announce": None,
        "announce_url": None,
        "apps": [] if hide_apps else _preview_apps(),
        "experience": experience,
    }


def _preview_apps() -> list[dict[str, Any]]:
    from app.models.settings import Platform

    return [
        {
            "name": "Preview App",
            "recommended": False,
            "platform": Platform.ANDROID,
            "icon_url": None,
            "description": {"en": "Sample recommended application block."},
            "download_links": [],
            "import_url": None,
        }
    ]


def render_customer_subscription_page_html(experience: dict[str, Any] | None) -> str:
    template = template_settings.subscription_page_template or "subscription/index.html"
    return render_template(template, subscription_preview_body_payload(experience))


async def resolve_customer_subscription_experience_for_preview(
    db: AsyncSession,
    *,
    workspace_id: int | None,
    use_active_template: bool,
    template_draft: dict[str, Any] | None,
) -> dict[str, Any] | None:
    try:
        if use_active_template:
            template_ctx = await resolve_subscription_experience_context(db, workspace_id=workspace_id)
        else:
            template_ctx = template_draft
        merged = await resolve_subscription_page_experience(
            db,
            workspace_id=workspace_id,
            experience_from_template=template_ctx,
        )
        return sanitize_experience_for_render(merged)
    except Exception:
        return None
