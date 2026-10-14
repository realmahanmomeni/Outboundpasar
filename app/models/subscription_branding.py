from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

_ALLOWED_URL_SCHEMES = re.compile(r"^https?://", re.IGNORECASE)


def _optional_http_url(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if not _ALLOWED_URL_SCHEMES.match(text):
        raise ValueError("URL must use http or https scheme")
    if len(text) > 2048:
        raise ValueError("URL is too long")
    return text


class SubscriptionBrandingUpsert(BaseModel):
    brand_name: str | None = Field(default=None, max_length=256)
    logo_url: str | None = Field(default=None, max_length=2048)
    support_url: str | None = Field(default=None, max_length=2048)
    telegram_url: str | None = Field(default=None, max_length=2048)
    welcome_message: str | None = Field(default=None, max_length=4096)
    footer_text: str | None = Field(default=None, max_length=4096)
    primary_color: str | None = Field(default=None, max_length=64)
    secondary_color: str | None = Field(default=None, max_length=64)

    @field_validator("logo_url", "support_url", "telegram_url")
    @classmethod
    def validate_urls(cls, value: str | None) -> str | None:
        return _optional_http_url(value)


class SubscriptionBrandingResponse(BaseModel):
    id: int
    workspace_id: int
    brand_name: str | None = None
    logo_url: str | None = None
    support_url: str | None = None
    telegram_url: str | None = None
    welcome_message: str | None = None
    footer_text: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_db(cls, row) -> SubscriptionBrandingResponse:
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            brand_name=row.brand_name,
            logo_url=row.logo_url,
            support_url=row.support_url,
            telegram_url=row.telegram_url,
            welcome_message=row.welcome_message,
            footer_text=row.footer_text,
            primary_color=row.primary_color,
            secondary_color=row.secondary_color,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def branding_experience_overlay(row) -> dict[str, Any]:
    """Map branding profile into subscription page experience context."""
    support_links: list[dict[str, str]] = []
    if row.support_url:
        support_links.append({"label": "Support", "url": row.support_url})
    if row.telegram_url:
        support_links.append({"label": "Telegram", "url": row.telegram_url})
    theme: dict[str, str] = {}
    if row.primary_color:
        theme["primary"] = row.primary_color
    if row.secondary_color:
        theme["accent"] = row.secondary_color
    return {
        "title": row.brand_name,
        "description": row.welcome_message,
        "logo_url": row.logo_url,
        "footer_text": row.footer_text,
        "theme": theme,
        "support_links": support_links,
        "custom_blocks": [],
        "branding": True,
    }
