import asyncio
from datetime import UTC, datetime as dt
from typing import Any

import aiohttp
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.db import GetDB
from app.db.models import Admin, User
from app.db.models_oc import OCIntegration, OCPanel, OCUserMapping
from app.operation.admin_sync import enforce_admin_limits_now
from app.utils.crypto import decrypt_secret
from app.utils.logger import get_logger

logger = get_logger("oc-usage")

OC_API_SEM = asyncio.Semaphore(10)


async def fetch_oc_user_usage(
    base_url: str,
    token: str,
    source_panel_id: str | int,
    external_user_id: str,
    session: aiohttp.ClientSession | None = None,
) -> int | None:
    """
    Fetch cumulative usage for a user on an Outbound Center panel.
    
    Returns cumulative total bytes (int) on success, or None on failure/not-found.
    """
    url = f"{base_url.rstrip('/')}/v1/integration/panels/{source_panel_id}/users/{external_user_id}/usage"
    headers = {"X-Integration-Token": token}
    timeout = aiohttp.ClientTimeout(total=15.0)

    owns_session = False
    if session is None:
        session = aiohttp.ClientSession(timeout=timeout)
        owns_session = True

    try:
        async with OC_API_SEM:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                status = resp.status
                if status == 200:
                    try:
                        data = await resp.json()
                        upload_bytes = max(0, int(data.get("upload_bytes") or 0))
                        download_bytes = max(0, int(data.get("download_bytes") or 0))
                        total_bytes = max(0, int(data.get("total_bytes") or 0))
                        cumulative = max(0, total_bytes, upload_bytes + download_bytes)
                        max_safe = 9_223_372_036_854_775_807
                        if cumulative > max_safe:
                            logger.warning(
                                "Cumulative traffic %s for %s exceeds max safe limit; capping",
                                cumulative,
                                external_user_id,
                            )
                            cumulative = max_safe
                        return cumulative
                    except (ValueError, TypeError, KeyError) as exc:
                        logger.warning(
                            "Malformed usage response for %s on panel %s: %s",
                            external_user_id,
                            source_panel_id,
                            exc,
                        )
                        return None

                if status == 404:
                    logger.debug(
                        "User mapping %s not found on panel %s (HTTP 404)",
                        external_user_id,
                        source_panel_id,
                    )
                    return None

                if status in (401, 403):
                    logger.error(
                        "Authentication failed for panel %s integration (HTTP %s)",
                        source_panel_id,
                        status,
                    )
                    return None

                logger.warning(
                    "OC panel API error HTTP %s for user %s on panel %s",
                    status,
                    external_user_id,
                    source_panel_id,
                )
                return None

    except aiohttp.ClientError as exc:
        logger.warning(
            "OC panel API request failed for %s on panel %s: %s",
            external_user_id,
            source_panel_id,
            exc,
        )
        return None
    except TimeoutError:
        logger.warning(
            "OC panel API timeout for %s on panel %s",
            external_user_id,
            source_panel_id,
        )
        return None
    except Exception as exc:
        logger.exception(
            "Unexpected error fetching usage for %s on panel %s: %s",
            external_user_id,
            source_panel_id,
            exc,
        )
        return None
    finally:
        if owns_session:
            await session.close()


async def account_mapping_usage(mapping_id: int, remote_cumulative: int) -> int:
    """
    Safely accounts remote cumulative usage for a single OCUserMapping.
    Uses row-level locking (with_for_update) to prevent concurrent double-accounting.
    
    Returns:
        int: Accounted (multiplied) delta added to user traffic.
    """
    async with GetDB() as db:
        stmt = (
            select(OCUserMapping)
            .options(selectinload(OCUserMapping.panel))
            .where(OCUserMapping.id == mapping_id)
            .with_for_update()
        )
        mapping = (await db.execute(stmt)).scalar_one_or_none()
        if not mapping:
            return 0

        # Only process active mappings
        if mapping.status != "active":
            return 0

        if remote_cumulative < 0:
            logger.warning(
                "Negative cumulative traffic %s for mapping %s ignored",
                remote_cumulative,
                mapping.id,
            )
            return 0

        previous = mapping.last_cumulative_traffic or 0
        panel_mult = float(mapping.panel.multiplier) if (mapping.panel and mapping.panel.multiplier is not None and mapping.panel.multiplier > 0) else 1.0
        multiplier = panel_mult if panel_mult > 0 else 1.0

        if remote_cumulative > previous:
            # Monotonic increase
            raw_delta = remote_cumulative - previous
            accounted_delta = max(0, int(raw_delta * multiplier))

            mapping.last_cumulative_traffic = remote_cumulative
            mapping.updated_at = dt.now(UTC)

            logger.info(
                "Accounted traffic for mapping %s (user %s, panel %s): raw_delta=%s, accounted_delta=%s (x%s), new_cumulative=%s",
                mapping.id,
                mapping.user_id,
                mapping.panel_id,
                raw_delta,
                accounted_delta,
                multiplier,
                remote_cumulative,
            )

        elif remote_cumulative < previous:
            # Remote reset detected
            raw_delta = max(0, remote_cumulative)
            accounted_delta = max(0, int(raw_delta * multiplier))

            logger.warning(
                "Remote reset detected for mapping %s (user %s, panel %s): previous=%s, current=%s, accounted_delta=%s (x%s)",
                mapping.id,
                mapping.user_id,
                mapping.panel_id,
                previous,
                remote_cumulative,
                accounted_delta,
                multiplier,
            )

            mapping.last_cumulative_traffic = remote_cumulative
            mapping.updated_at = dt.now(UTC)

        else:
            # Zero delta (remote_cumulative == previous)
            logger.debug(
                "Zero delta for mapping %s (user %s, panel %s): cumulative=%s",
                mapping.id,
                mapping.user_id,
                mapping.panel_id,
                remote_cumulative,
            )
            return 0

        if accounted_delta > 0:
            user_stmt = (
                update(User)
                .where(User.id == mapping.user_id)
                .values(
                    used_traffic=User.used_traffic + accounted_delta,
                    online_at=dt.now(UTC),
                )
                .execution_options(synchronize_session=False)
            )
            await db.execute(user_stmt)

            admin_id = (
                await db.execute(select(User.admin_id).where(User.id == mapping.user_id))
            ).scalar_one_or_none()

            if admin_id:
                admin_stmt = (
                    update(Admin)
                    .where(Admin.id == admin_id)
                    .values(used_traffic=Admin.used_traffic + accounted_delta)
                    .execution_options(synchronize_session=False)
                )
                await db.execute(admin_stmt)

        await db.commit()

        if accounted_delta > 0:
            try:
                from app.operation.access_control import enforce_user_limits_now

                await enforce_user_limits_now(user_ids=[mapping.user_id], logger=logger)
            except Exception:
                logger.exception("Failed to enforce user limits after accounting mapping usage")

        return accounted_delta


async def record_oc_user_usages(session: aiohttp.ClientSession | None = None) -> int:
    """
    Collects and accounts usage for all active Outbound Center user mappings.
    Returns total accounted traffic delta across all users.
    """
    async with GetDB() as db:
        stmt = (
            select(OCUserMapping)
            .options(
                selectinload(OCUserMapping.panel).selectinload(OCPanel.integration)
            )
            .where(OCUserMapping.status == "active")
        )
        mappings = (await db.execute(stmt)).scalars().all()

    if not mappings:
        return 0

    # Cache decrypted tokens per integration to avoid repeated decryption
    token_cache: dict[int, str | None] = {}
    valid_targets: list[tuple[OCUserMapping, str, str]] = []

    for mapping in mappings:
        panel = mapping.panel
        if not panel or not panel.integration or not panel.integration.is_active:
            continue

        integ = panel.integration
        if integ.id not in token_cache:
            try:
                decrypted = await decrypt_secret(integ.api_token_encrypted)
                token_cache[integ.id] = decrypted
            except Exception as exc:
                logger.error("Failed to decrypt API token for integration %s: %s", integ.id, exc)
                token_cache[integ.id] = None

        token = token_cache.get(integ.id)
        if not token:
            continue

        valid_targets.append((mapping, integ.base_url, token))

    if not valid_targets:
        return 0

    owns_session = False
    if session is None:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20.0))
        owns_session = True

    total_accounted = 0
    try:
        async def _fetch_and_account(target: tuple[OCUserMapping, str, str]) -> int:
            mapping, base_url, token = target
            try:
                remote_cumulative = await fetch_oc_user_usage(
                    base_url=base_url,
                    token=token,
                    source_panel_id=mapping.panel.source_panel_id,
                    external_user_id=mapping.external_user_id,
                    session=session,
                )
                if remote_cumulative is not None:
                    return await account_mapping_usage(mapping.id, remote_cumulative)
            except Exception as exc:
                logger.warning(
                    "Failed processing usage for mapping %s (panel %s): %s",
                    mapping.id,
                    mapping.panel_id,
                    exc,
                )
            return 0

        tasks = [_fetch_and_account(target) for target in valid_targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, int) and res > 0:
                total_accounted += res

        if total_accounted > 0:
            try:
                await enforce_admin_limits_now(logger=logger)
            except Exception:
                logger.exception("Failed to enforce admin limits after OC usage collection")
            try:
                from app.operation.access_control import enforce_user_limits_now

                await enforce_user_limits_now(logger=logger)
            except Exception:
                logger.exception("Failed to enforce user limits after OC usage collection")

    finally:
        if owns_session:
            await session.close()

    return total_accounted
