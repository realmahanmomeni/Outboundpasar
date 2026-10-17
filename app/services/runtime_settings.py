"""Workspace-scoped runtime settings reads with isolated caching (Phase 2-A foundation)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiocache import cached
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import GetDB
from app.db.crud.settings import get_global_settings, get_settings_by_workspace
from app.models.settings import SettingsSchema

GLOBAL_SCOPE_KEY = "global"
SETTINGS_REFRESH_ACTION = "refresh"


class WorkspaceSettingsNotFoundError(LookupError):
    """Raised when an explicit workspace_id has no settings row (no global fallback)."""

    def __init__(self, workspace_id: int) -> None:
        self.workspace_id = workspace_id
        super().__init__(f"Workspace settings not found for workspace_id={workspace_id}")


def runtime_settings_cache_key(workspace_id: int | None) -> str:
    scope = GLOBAL_SCOPE_KEY if workspace_id is None else f"ws:{workspace_id}"
    return f"app.services.runtime_settings._load_runtime_settings_snapshot:{scope}"


def _snapshot_key_builder(func, *args, **kwargs) -> str:
    workspace_id = args[0] if args else kwargs.get("workspace_id")
    return runtime_settings_cache_key(workspace_id)


@dataclass(frozen=True, slots=True)
class RuntimeSettingsSnapshot:
    """Validated settings for one scope. ``workspace_id`` is None for the platform-global row."""

    workspace_id: int | None
    settings: SettingsSchema


@dataclass(frozen=True, slots=True)
class SettingsRefreshEvent:
    """Parsed NATS / local settings cache invalidation instruction."""

    workspace_id: int | None
    legacy_global_message: bool


async def _load_runtime_settings_snapshot_from_db(db: AsyncSession, workspace_id: int | None) -> SettingsSchema:
    if workspace_id is None:
        row = await get_global_settings(db)
        if row is None:
            raise ValueError("Settings not configured")
    else:
        row = await get_settings_by_workspace(db, workspace_id)
        if row is None:
            raise WorkspaceSettingsNotFoundError(workspace_id)
    return SettingsSchema.model_validate(row)


@cached(key_builder=_snapshot_key_builder)
async def _load_runtime_settings_snapshot(workspace_id: int | None) -> SettingsSchema:
    async with GetDB() as db:
        return await _load_runtime_settings_snapshot_from_db(db, workspace_id)


async def get_runtime_settings_snapshot(
    workspace_id: int | None,
    *,
    db: AsyncSession | None = None,
    cache_read: bool = True,
) -> RuntimeSettingsSnapshot:
    """
    Load runtime settings for a single scope.

    ``workspace_id=None`` selects only the platform-global row.
    A non-null ``workspace_id`` never falls back to global settings.
    """
    if db is not None:
        settings = await _load_runtime_settings_snapshot_from_db(db, workspace_id)
    elif cache_read:
        settings = await _load_runtime_settings_snapshot(workspace_id)
    else:
        async with GetDB() as session:
            settings = await _load_runtime_settings_snapshot_from_db(session, workspace_id)
    return RuntimeSettingsSnapshot(workspace_id=workspace_id, settings=settings)


async def clear_runtime_settings_cache(
    workspace_id: int | None = None,
    *,
    all_scopes: bool = False,
) -> None:
    cache = _load_runtime_settings_snapshot.cache
    if all_scopes:
        await cache.clear()
        return
    await cache.delete(runtime_settings_cache_key(workspace_id))


def _parse_workspace_id_value(raw: Any) -> int | None:
    """
    Parse ``workspace_id`` when the JSON key is present.

    Returns ``None`` for explicit JSON null (global refresh).
    Raises ``ValueError`` for invalid types or values (no coercion).
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError("workspace_id must not be a boolean")
    if type(raw) is not int:
        raise ValueError("workspace_id must be a JSON integer")
    if raw <= 0:
        raise ValueError("workspace_id must be a positive integer")
    return raw


def parse_settings_refresh_message(payload: dict[str, Any] | None) -> SettingsRefreshEvent | None:
    """
    Interpret settings refresh messages for NATS ``MessageTopic.SETTING``.

    Accepted:
    - ``{"action": "refresh"}`` — legacy global refresh (``workspace_id`` key absent).
    - ``{"action": "refresh", "workspace_id": null}`` — explicit global refresh.
    - ``{"action": "refresh", "workspace_id": <positive int>}`` — workspace-scoped refresh.

    Rejected (returns ``None``, no cache or bot side effects):
    - Missing or unknown ``action``, non-string ``action``, or any action other than ``refresh``.
    - ``workspace_id`` present but not JSON null and not a positive JSON integer (no bool/float/string coercion).
    - Non-dict payloads.
    """
    if not isinstance(payload, dict):
        return None

    action = payload.get("action")
    if action is None or type(action) is not str or action != SETTINGS_REFRESH_ACTION:
        return None

    if "workspace_id" not in payload:
        return SettingsRefreshEvent(workspace_id=None, legacy_global_message=True)

    try:
        workspace_id = _parse_workspace_id_value(payload["workspace_id"])
    except ValueError:
        return None

    if workspace_id is None:
        return SettingsRefreshEvent(workspace_id=None, legacy_global_message=False)
    return SettingsRefreshEvent(workspace_id=workspace_id, legacy_global_message=False)
