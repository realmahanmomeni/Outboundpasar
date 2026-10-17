"""Protect Outbound Center virtual inbounds from the core-config inbound cleanup job.

Imported OC destination hosts are represented by virtual inbounds (``oc_<panel>_d_<id>``)
that never exist in the Xray core config.  The periodic ``remove_old_inbounds`` job removes
every inbound whose tag is absent from the core config; without this guard it deleted the
virtual inbounds every cycle, which

* cleared Group memberships (``ProxyInbound`` ``after_delete`` drops association rows),
* set ``hosts.inbound_tag`` / ``oc_panel_destination_hosts.virtual_inbound_tag`` to NULL, and
* made the OC reconcile job delete the provisioned external user (configs stopped working).

Virtual inbounds are owned by OC panel rows and are removed explicitly through
``app.services.oc_destination_host_lifecycle`` (host delete / panel delete), never by the
core-config diff.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import OCPanelConfig, OCPanelDestinationHost
from app.services.oc_share_link import destination_virtual_inbound_tag


async def protected_oc_inbound_tags(db: AsyncSession) -> set[str]:
    """Virtual inbound tags still owned by an OC destination row or catalog config row."""
    protected: set[str] = set()

    dest_rows = (
        await db.execute(
            select(
                OCPanelDestinationHost.panel_id,
                OCPanelDestinationHost.destination_config_id,
                OCPanelDestinationHost.virtual_inbound_tag,
            )
        )
    ).all()
    for panel_id, destination_config_id, virtual_tag in dest_rows:
        if virtual_tag:
            protected.add(virtual_tag)
        if destination_config_id:
            # The stored FK may be NULL after an earlier deletion; the canonical tag is derivable.
            protected.add(destination_virtual_inbound_tag(panel_id, destination_config_id))

    catalog_tags = (
        await db.execute(
            select(OCPanelConfig.virtual_inbound_tag).where(OCPanelConfig.virtual_inbound_tag.is_not(None))
        )
    ).scalars().all()
    protected.update(tag for tag in catalog_tags if tag)
    return protected
