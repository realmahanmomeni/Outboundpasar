"""Shared helpers for Phase 1–9 workspace isolation scripts (standalone, not pytest)."""
from __future__ import annotations

import uuid


def unique_username(prefix: str = "adm") -> str:
    """Globally unique username (max 34 chars for admins.username) safe across repeated phase script runs."""
    safe = prefix.replace(" ", "_")[:8]
    # prefix + underscore + hex must fit VARCHAR(34)
    suffix_len = max(8, 33 - len(safe))
    return f"{safe}_{uuid.uuid4().hex[:suffix_len]}"


def unique_tag(prefix: str = "x") -> str:
    """Unique suffix for groups, templates, tenants, etc."""
    safe = prefix.replace(" ", "_")[:24]
    return f"{safe}_{uuid.uuid4().hex[:12]}"
