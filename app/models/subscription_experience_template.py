from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SubscriptionExperienceTheme(BaseModel):
    primary: str | None = Field(default=None, max_length=64)
    background: str | None = Field(default=None, max_length=64)
    foreground: str | None = Field(default=None, max_length=64)
    card: str | None = Field(default=None, max_length=64)
    accent: str | None = Field(default=None, max_length=64)


class SubscriptionExperienceSupportLink(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)


class SubscriptionExperienceCustomBlock(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(max_length=8192)


class SubscriptionExperienceTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    is_enabled: bool = True
    title: str | None = Field(default=None, max_length=512)
    description: str | None = Field(default=None, max_length=4096)
    logo_url: str | None = Field(default=None, max_length=2048)
    theme: SubscriptionExperienceTheme | None = None
    support_links: list[SubscriptionExperienceSupportLink] = Field(default_factory=list)
    footer_text: str | None = Field(default=None, max_length=4096)
    custom_blocks: list[SubscriptionExperienceCustomBlock] = Field(default_factory=list)


class SubscriptionExperienceTemplateModify(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    is_enabled: bool | None = None
    title: str | None = Field(default=None, max_length=512)
    description: str | None = Field(default=None, max_length=4096)
    logo_url: str | None = Field(default=None, max_length=2048)
    theme: SubscriptionExperienceTheme | None = None
    support_links: list[SubscriptionExperienceSupportLink] | None = None
    footer_text: str | None = Field(default=None, max_length=4096)
    custom_blocks: list[SubscriptionExperienceCustomBlock] | None = None


class SubscriptionExperienceTemplateResponse(BaseModel):
    id: int
    name: str
    is_enabled: bool
    is_active: bool = False
    title: str | None = None
    description: str | None = None
    logo_url: str | None = None
    theme: SubscriptionExperienceTheme | None = None
    support_links: list[SubscriptionExperienceSupportLink] = Field(default_factory=list)
    footer_text: str | None = None
    custom_blocks: list[SubscriptionExperienceCustomBlock] = Field(default_factory=list)
    created_at: datetime

    @classmethod
    def from_db(cls, row, *, active_template_id: int | None = None) -> SubscriptionExperienceTemplateResponse:
        theme = row.theme if isinstance(row.theme, dict) else None
        support_links = row.support_links if isinstance(row.support_links, list) else []
        custom_blocks = row.custom_blocks if isinstance(row.custom_blocks, list) else []
        return cls(
            id=row.id,
            name=row.name,
            is_enabled=bool(row.is_enabled),
            is_active=active_template_id is not None and row.id == active_template_id,
            title=row.title,
            description=row.description,
            logo_url=row.logo_url,
            theme=SubscriptionExperienceTheme.model_validate(theme) if theme else None,
            support_links=[SubscriptionExperienceSupportLink.model_validate(item) for item in support_links],
            footer_text=row.footer_text,
            custom_blocks=[SubscriptionExperienceCustomBlock.model_validate(item) for item in custom_blocks],
            created_at=row.created_at,
        )


class SubscriptionExperienceTemplateListResponse(BaseModel):
    templates: list[SubscriptionExperienceTemplateResponse]


class SubscriptionExperienceActivateRequest(BaseModel):
    template_id: int | None = Field(
        default=None,
        description="Set active template for workspace; null clears selection.",
    )


class SubscriptionExperiencePreviewRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    logo_url: str | None = None
    theme: SubscriptionExperienceTheme | None = None
    support_links: list[SubscriptionExperienceSupportLink] = Field(default_factory=list)
    footer_text: str | None = None
    custom_blocks: list[SubscriptionExperienceCustomBlock] = Field(default_factory=list)


class SubscriptionExperiencePreviewResponse(BaseModel):
    html: str


def experience_context_from_row(row) -> dict[str, Any]:
    """Jinja context fragment for subscription page rendering."""
    return {
        "title": row.title,
        "description": row.description,
        "logo_url": row.logo_url,
        "theme": row.theme if isinstance(row.theme, dict) else {},
        "support_links": row.support_links if isinstance(row.support_links, list) else [],
        "footer_text": row.footer_text,
        "custom_blocks": row.custom_blocks if isinstance(row.custom_blocks, list) else [],
    }
