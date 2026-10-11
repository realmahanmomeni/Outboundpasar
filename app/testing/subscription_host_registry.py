"""Register specific ProxyHost rows in HostManager without loading the full DB catalog."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.hosts import host_manager
from app.db.models import ProxyHost
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig


def _subscription_data_from_proxy_host(row: ProxyHost) -> SubscriptionInboundData | None:
    tag = (row.inbound.tag if row.inbound else None) or row.inbound_tag
    if not tag:
        return None
    address = row.address
    if isinstance(address, set):
        address = next(iter(address), "127.0.0.1")
    elif isinstance(address, list):
        address = address[0] if address else "127.0.0.1"
    return SubscriptionInboundData(
        remark=row.remark,
        inbound_tag=tag,
        protocol="vless",
        address=[address] if isinstance(address, str) else address,
        port=[row.port or 443],
        network="tcp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(),
        encryption="none",
    )


async def register_proxy_hosts_for_subscription(db: AsyncSession, host_ids: list[int]) -> None:
    """
    Add only the given hosts to the in-process HostManager (Phase 15 / isolated tests).

    Avoids ``host_manager.setup(db)`` which loads every ProxyHost and can fail on unrelated rows.
    """
    if not host_ids:
        return
    rows = (
        await db.execute(
            select(ProxyHost)
            .options(selectinload(ProxyHost.inbound))
            .where(ProxyHost.id.in_(host_ids))
        )
    ).scalars().all()
    prepared: list[tuple[int, SubscriptionInboundData]] = []
    for row in rows:
        data = _subscription_data_from_proxy_host(row)
        if data is not None:
            prepared.append((row.id, data))
    if prepared:
        await host_manager._add_prepared_hosts_local(prepared)
