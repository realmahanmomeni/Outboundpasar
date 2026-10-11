from sqlalchemy.ext.asyncio import AsyncSession

from app import scheduler
from app.core.manager import core_manager
from app.db import GetDB
from app.db.crud.host import get_inbounds_not_in_tags, remove_inbounds
from app.services.oc_inbound_protection import protected_oc_inbound_tags
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("jobs")


async def remove_stale_inbounds(db: AsyncSession, in_use_inbounds: list[str]) -> list[str]:
    """
    Remove inbounds that are neither in the core config nor owned by an OC panel row.

    An empty ``in_use_inbounds`` list means the core configuration is not loaded (startup race,
    transient cache/NATS failure); it is never treated as "delete every inbound".
    """
    if not in_use_inbounds:
        logger.warning("remove_old_inbounds skipped: core inbound list is empty (core config not loaded)")
        return []

    protected = await protected_oc_inbound_tags(db)
    old_inbounds = await get_inbounds_not_in_tags(db, in_use_inbounds, protected_tags=protected)
    removed_tags = [inbound.tag for inbound in old_inbounds]

    await remove_inbounds(db, old_inbounds)

    for tag in removed_tags:
        logger.info(f"inbound {tag} removed.")
    return removed_tags


async def remove_old_inbounds():
    await core_manager._reload_from_cache()
    in_use_inbounds = await core_manager.get_inbounds()

    async with GetDB() as db:
        await remove_stale_inbounds(db, in_use_inbounds)


if runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        remove_old_inbounds,
        "interval",
        seconds=job_settings.remove_old_inbounds_interval,
        coalesce=True,
        max_instances=1,
        id="remove_old_inbounds",
        replace_existing=True,
    )
