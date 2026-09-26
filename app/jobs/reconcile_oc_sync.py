import asyncio
from sqlalchemy import select

from app import scheduler
from app.db import GetDB
from app.db.models import User
from app.db.models_oc import OCUserMapping
from app.node.oc_sync import enqueue_oc_user_sync
from app.utils.logger import get_logger
from config import runtime_settings

logger = get_logger("oc-reconcile")

async def reconcile_all_oc_users():
    async with GetDB() as db:
        # Fetch users with active/on_hold status OR those with an existing OCUserMapping
        users_with_mappings = (await db.execute(
            select(User)
            .where(User.id.in_(select(OCUserMapping.user_id)))
        )).scalars().all()
        
        active_users = (await db.execute(
            select(User)
            .where(User.status.in_(["active", "on_hold"]))
        )).scalars().all()
        
        user_map = {u.id: u for u in users_with_mappings}
        for u in active_users:
            if u.id not in user_map:
                user_map[u.id] = u
                
        for user in user_map.values():
            try:
                # `enqueue_oc_user_sync` natively calculates desired state vs last_synced_configs 
                # and gracefully NO-OPs if there's no drift.
                await enqueue_oc_user_sync(db, user)
            except Exception as e:
                logger.error(f"Failed to reconcile user {user.username}: {e}")
                
        await db.commit()

if runtime_settings.role.runs_scheduler:
    # Run periodically to ensure drift is caught and failed jobs are retried
    scheduler.add_job(
        reconcile_all_oc_users,
        "interval",
        coalesce=True,
        minutes=5,
        max_instances=1,
        id="reconcile_all_oc_users",
        replace_existing=True,
    )
