"""Workspace branding overlay for subscription HTML pages (Phase 3-D)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.subscription_branding import get_branding_for_workspace
from app.models.subscription_branding import branding_experience_overlay
from config import subscription_branding_settings


def merge_experience_with_branding(
    experience: dict[str, Any] | None,
    branding_overlay: dict[str, Any],
) -> dict[str, Any]:
    """Branding fills gaps; explicit experience template fields win when both set."""
    base = dict(experience or {})
    merged = {**branding_overlay, **{k: v for k, v in base.items() if v not in (None, "", [], {})}}
    theme = {**(branding_overlay.get("theme") or {}), **(base.get("theme") or {})}
    if theme:
        merged["theme"] = theme
    links = list(branding_overlay.get("support_links") or [])
    existing = base.get("support_links") or []
    if existing:
        merged["support_links"] = existing
    elif links:
        merged["support_links"] = links
    return merged


async def resolve_branding_page_overlay(
    db: AsyncSession,
    *,
    workspace_id: int | None,
) -> dict[str, Any] | None:
    if not subscription_branding_settings.enabled or workspace_id is None:
        return None
    row = await get_branding_for_workspace(db, workspace_id)
    if row is None:
        return None
    return branding_experience_overlay(row)


async def resolve_subscription_page_experience(
    db: AsyncSession,
    *,
    workspace_id: int | None,
    experience_from_template: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Combine Phase 3-C template experience with Phase 3-D branding when enabled."""
    branding = await resolve_branding_page_overlay(db, workspace_id=workspace_id)
    if branding is None:
        return experience_from_template
    if experience_from_template is None:
        return branding
    return merge_experience_with_branding(experience_from_template, branding)
