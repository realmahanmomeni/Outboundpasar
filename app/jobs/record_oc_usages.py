"""Outbound Center traffic collection (panel deployments must run this without node workers)."""

from __future__ import annotations

from datetime import UTC, datetime as dt, timedelta as td

from app import scheduler
from app.node.oc_usage import record_oc_user_usages
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("record-oc-usages")

_oc_usage_running = False


async def record_oc_usages_job() -> None:
    global _oc_usage_running
    if _oc_usage_running:
        logger.warning("record_oc_usages skipped; previous run still in progress")
        return
    _oc_usage_running = True
    try:
        total = await record_oc_user_usages()
        if total:
            logger.debug("Outbound Center usage accounted: %s bytes (multiplier applied per mapping)", total)
    except Exception:
        logger.exception("Outbound Center user usage recording failed")
    finally:
        _oc_usage_running = False


if runtime_settings.role.runs_panel or runtime_settings.role.runs_node:
    scheduler.add_job(
        record_oc_usages_job,
        "interval",
        seconds=job_settings.record_user_usages_interval,
        start_date=dt.now(UTC) + td(seconds=35),
        coalesce=True,
        max_instances=1,
        id="record_oc_usages",
        replace_existing=True,
    )
