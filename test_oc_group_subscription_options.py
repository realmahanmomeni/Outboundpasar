"""Group picker uses destination subscription host names."""
from __future__ import annotations

import asyncio

from app.db import GetDB
from app.services.assignable_hosts import list_group_host_options
from app.services.oc_subscription_config_label import destination_subscription_config_name
from app.db.models_oc import OCPanelConfig


def test_label_helper_not_source_name():
    cfg = OCPanelConfig(
        panel_id=1,
        source_config_id="HTTP-UP",
        source_name="HTTP-UP",
        virtual_inbound_tag="oc_1_HTTP-UP",
        source_payload={
            "subscription_link": "vless://u@1.1.1.1:443?encryption=none#Real%20Name",
            "subscription_parsed": {"remark": "🇩🇪 Germany | VLESS"},
        },
    )
    assert destination_subscription_config_name(cfg) == "🇩🇪 Germany | VLESS"


async def main():
    test_label_helper_not_source_name()
    async with GetDB() as db:
        opts = await list_group_host_options(db)
        for o in opts:
            if o.inbound_tag.startswith("oc_") and "_d_" in o.inbound_tag:
                assert o.display_name not in ("HTTP-UP", "VLESS TCP", "Shadowsocks TCP")
    print("test_oc_group_subscription_options: OK")


if __name__ == "__main__":
    asyncio.run(main())
