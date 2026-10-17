from datetime import UTC, datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_object_session
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.crud.user import _review_user_select_stmt
from app.db.models import Admin, AdminStatus, User, UserStatus
from app.utils.logger import get_logger

logger = get_logger("access-control")


def is_user_access_allowed(
    user: User,
    admin: Admin | None = None,
    now: dt | None = None,
) -> bool:
    """
    Authoritative synchronous deterministic access decision for a PasarGuard user.

    Returns True (Allowed) IF:
      - user status is active or on_hold according to existing semantics
      - user is not expired
      - user has not exceeded their traffic limit
      - applicable admin limits allow access
    Otherwise returns False (Restricted).
    """
    if now is None:
        now = dt.now(UTC)

    # 1. User status check
    status = getattr(user, "status", None)
    if status not in (UserStatus.active, UserStatus.on_hold):
        return False

    # 2. Expiration check
    expire = getattr(user, "expire", None)
    if expire is not None:
        expire_utc = expire if expire.tzinfo is not None else expire.replace(tzinfo=UTC)
        if expire_utc <= now:
            return False

    # 3. Traffic limit check (0 or None means unlimited)
    data_limit = getattr(user, "data_limit", None)
    used_traffic = getattr(user, "used_traffic", 0) or 0
    if data_limit is not None and data_limit > 0 and used_traffic >= data_limit:
        return False

    # 4. Applicable admin limits check
    admin_id = getattr(user, "admin_id", None)
    if admin_id is not None:
        if admin is None:
            admin = getattr(user, "__dict__", {}).get("admin")

        if admin is not None:
            admin_status = getattr(admin, "status", None)
            role = getattr(admin, "__dict__", {}).get("role")

            if admin_status == AdminStatus.disabled:
                if role is None or getattr(role, "disconnect_users_when_disabled", True):
                    return False

            if admin_status == AdminStatus.limited:
                if role is None or getattr(role, "disconnect_users_when_limited", True):
                    return False

            admin_limit = getattr(admin, "data_limit", None)
            admin_used = getattr(admin, "used_traffic", 0) or 0
            if admin_limit is not None and admin_limit > 0 and admin_used >= admin_limit:
                if role is None or getattr(role, "disconnect_users_when_limited", True):
                    return False

    return True


async def check_user_access_allowed(
    user: User,
    db: AsyncSession | None = None,
    now: dt | None = None,
) -> bool:
    """
    Authoritative asynchronous deterministic access decision for a PasarGuard user.
    Safely resolves admin and role relations if not already loaded in memory.
    """
    if now is None:
        now = dt.now(UTC)

    # 1. User status check
    status = getattr(user, "status", None)
    if status is None:
        try:
            status = await user.awaitable_attrs.status
        except (AttributeError, Exception):
            status = None
    if status not in (UserStatus.active, UserStatus.on_hold):
        return False

    # 2. Expiration check
    expire = getattr(user, "expire", None)
    if expire is not None:
        expire_utc = expire if expire.tzinfo is not None else expire.replace(tzinfo=UTC)
        if expire_utc <= now:
            return False

    # 3. Traffic limit check (0 or None means unlimited)
    data_limit = getattr(user, "data_limit", None)
    used_traffic = getattr(user, "used_traffic", 0) or 0
    if data_limit is not None and data_limit > 0 and used_traffic >= data_limit:
        return False

    # 4. Applicable admin limits check
    admin_id = getattr(user, "admin_id", None)
    if admin_id is not None:
        admin = getattr(user, "__dict__", {}).get("admin")
        if admin is None:
            try:
                admin = await user.awaitable_attrs.admin
            except (AttributeError, Exception):
                admin = None

        if admin is None:
            target_session = db or async_object_session(user)
            if target_session is not None:
                stmt = select(Admin).options(selectinload(Admin.role)).where(Admin.id == admin_id)
                admin = (await target_session.execute(stmt)).scalar_one_or_none()

        if admin is not None:
            role = getattr(admin, "__dict__", {}).get("role")
            if role is None:
                try:
                    role = await admin.awaitable_attrs.role
                except (AttributeError, Exception):
                    role = None

            admin_status = getattr(admin, "status", None)
            if admin_status == AdminStatus.disabled:
                if role is None or getattr(role, "disconnect_users_when_disabled", True):
                    return False

            if admin_status == AdminStatus.limited:
                if role is None or getattr(role, "disconnect_users_when_limited", True):
                    return False

            admin_limit = getattr(admin, "data_limit", None)
            admin_used = getattr(admin, "used_traffic", 0) or 0
            if admin_limit is not None and admin_limit > 0 and admin_used >= admin_limit:
                if role is None or getattr(role, "disconnect_users_when_limited", True):
                    return False

    return True


def evaluate_user_access(
    user: User,
    admin: Admin | None = None,
    now: dt | None = None,
) -> dict[str, Any]:
    """
    Evaluates whether a user should have access, returning a comprehensive breakdown:
      - allowed: bool
      - reason: str | None
      - status: str
      - is_expired: bool
      - is_limited: bool
      - admin_blocked: bool
    """
    if now is None:
        now = dt.now(UTC)

    status = getattr(user, "status", None)
    status_val = status.value if hasattr(status, "value") else str(status)

    expire = getattr(user, "expire", None)
    is_expired = False
    if expire is not None:
        expire_utc = expire if expire.tzinfo is not None else expire.replace(tzinfo=UTC)
        is_expired = expire_utc <= now

    data_limit = getattr(user, "data_limit", None)
    used_traffic = getattr(user, "used_traffic", 0) or 0
    is_limited = bool(data_limit is not None and data_limit > 0 and used_traffic >= data_limit)

    admin_blocked = False
    admin_reason = None
    admin_id = getattr(user, "admin_id", None)
    if admin_id is not None:
        if admin is None:
            admin = getattr(user, "__dict__", {}).get("admin")

        if admin is not None:
            admin_status = getattr(admin, "status", None)
            role = getattr(admin, "__dict__", {}).get("role")

            if admin_status == AdminStatus.disabled:
                if role is None or getattr(role, "disconnect_users_when_disabled", True):
                    admin_blocked = True
                    admin_reason = "admin_disabled"

            elif admin_status == AdminStatus.limited:
                if role is None or getattr(role, "disconnect_users_when_limited", True):
                    admin_blocked = True
                    admin_reason = "admin_limited"

            else:
                admin_limit = getattr(admin, "data_limit", None)
                admin_used = getattr(admin, "used_traffic", 0) or 0
                if admin_limit is not None and admin_limit > 0 and admin_used >= admin_limit:
                    if role is None or getattr(role, "disconnect_users_when_limited", True):
                        admin_blocked = True
                        admin_reason = "admin_data_limit_exceeded"

    allowed = True
    reason = None

    if status not in (UserStatus.active, UserStatus.on_hold):
        allowed = False
        reason = f"status_{status_val}"
    elif is_expired:
        allowed = False
        reason = "expired"
    elif is_limited:
        allowed = False
        reason = "data_limit_exceeded"
    elif admin_blocked:
        allowed = False
        reason = admin_reason

    return {
        "allowed": allowed,
        "reason": reason,
        "status": status_val,
        "is_expired": is_expired,
        "is_limited": is_limited,
        "admin_blocked": admin_blocked,
    }


async def limit_exceeded_users(
    db: AsyncSession,
    user_ids: list[int] | set[int] | None = None,
    logger=None,
) -> int:
    """
    Enforces data limits for active/on_hold users whose used_traffic >= data_limit.
    Applies existing PasarGuard status transition (UserStatus.limited or next_plan reset),
    syncs native nodes (dropping access), and enqueues Phase 10 OC reconciliation.
    """
    stmt = _review_user_select_stmt().where(
        User.status.in_([UserStatus.active, UserStatus.on_hold]),
        User.is_limited,
    )
    if user_ids:
        stmt = stmt.where(User.id.in_(list(user_ids)))

    limited_users = list((await db.execute(stmt)).unique().scalars().all())
    if not limited_users:
        return 0

    from app.jobs.review_users import apply_status_changes

    await apply_status_changes(db, limited_users, UserStatus.limited)
    if logger:
        logger.info(f"Enforced data limits on {len(limited_users)} users")
    return len(limited_users)


async def enforce_user_limits_now(
    user_ids: list[int] | set[int] | None = None,
    logger=None,
) -> int:
    """
    Flip active/on_hold users that exceeded data_limit without waiting for the scheduler tick.
    Safe to call immediately after native or OC traffic accounting.
    """
    async with GetDB() as db:
        return await limit_exceeded_users(db, user_ids=user_ids, logger=logger)


async def expired_users_enforce(
    db: AsyncSession,
    user_ids: list[int] | set[int] | None = None,
    logger=None,
) -> int:
    """
    Enforces expiration for active/on_hold users whose expire <= now.
    Applies existing PasarGuard status transition (UserStatus.expired or next_plan reset),
    syncs native nodes (dropping access), and enqueues Phase 10 OC reconciliation.
    """
    stmt = _review_user_select_stmt().where(
        User.status.in_([UserStatus.active, UserStatus.on_hold]),
        User.is_expired,
    )
    if user_ids:
        stmt = stmt.where(User.id.in_(list(user_ids)))

    expired_users = list((await db.execute(stmt)).unique().scalars().all())
    if not expired_users:
        return 0

    from app.jobs.review_users import apply_status_changes

    await apply_status_changes(db, expired_users, UserStatus.expired)
    if logger:
        logger.info(f"Enforced expiration on {len(expired_users)} users")
    return len(expired_users)


async def enforce_user_expirations_now(
    user_ids: list[int] | set[int] | None = None,
    logger=None,
) -> int:
    """
    Flip active/on_hold users that expired without waiting for the scheduler tick.
    """
    async with GetDB() as db:
        return await expired_users_enforce(db, user_ids=user_ids, logger=logger)
