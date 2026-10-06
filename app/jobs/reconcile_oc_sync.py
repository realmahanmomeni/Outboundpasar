import asyncio
from sqlalchemy import or_, select

from app import scheduler
from app.db import GetDB
from app.db.models import User
from app.db.models_oc import OCUserMapping
from app.node.oc_sync import enqueue_oc_user_sync
from app.services.oc_user_mapping_state import OC_MAPPING_STATUS_ACTIVE
from app.utils.logger import get_logger
from config import runtime_settings

logger = get_logger("oc-reconcile")

_RECONCILE_BATCH = 500


async def _broken_mapping_user_ids(db, limit: int) -> list[int]:
    """Users with mappings that likely need repair (SQL pre-filter)."""
    rows = (
        await db.execute(
            select(OCUserMapping.user_id)
            .where(
                or_(
                    OCUserMapping.status != OC_MAPPING_STATUS_ACTIVE,
                    OCUserMapping.external_subscription_url.is_(None),
                    OCUserMapping.external_subscription_url == "",
                    OCUserMapping.last_synced_configs.is_(None),
                )
            )
            .distinct()
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def reconcile_all_oc_users():
    async with GetDB() as db:
        priority_ids = await _broken_mapping_user_ids(db, _RECONCILE_BATCH)

        users_with_mappings = (
            await db.execute(select(User).where(User.id.in_(select(OCUserMapping.user_id))))
        ).scalars().all()

        active_users = (
            await db.execute(select(User).where(User.status.in_(["active", "on_hold"])))
        ).scalars().all()

        user_map: dict[int, User] = {u.id: u for u in users_with_mappings}
        for u in active_users:
            if u.id not in user_map:
                user_map[u.id] = u

        seen: set[int] = set()
        for user_id in priority_ids:
            user = user_map.get(user_id)
            if user is None:
                continue
            seen.add(user_id)
            try:
                await enqueue_oc_user_sync(db, user)
            except Exception as e:
                logger.error(
                    "OC_RECONCILE_ERROR user_id=%s username=%s error=%s",
                    user.id,
                    user.username,
                    e,
                )

        for user in user_map.values():
            if user.id in seen:
                continue
            try:
                await enqueue_oc_user_sync(db, user)
            except Exception as e:
                logger.error(
                    "OC_RECONCILE_ERROR user_id=%s username=%s error=%s",
                    user.id,
                    user.username,
                    e,
                )

        await db.commit()


if runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        reconcile_all_oc_users,
        "interval",
        coalesce=True,
        minutes=5,
        max_instances=1,
        id="reconcile_all_oc_users",
        replace_existing=True,
    )
