"""Subscription import accepts valid links regardless of remark text."""
from __future__ import annotations

import asyncio

from app.services.oc_share_link import (
    decode_subscription_links,
    is_importable_configuration_link,
    subscription_config_links,
)


def test_remark_variants_imported():
    links = [
        "vless://u1@1.1.1.1:443?encryption=none#Shadowsocks%20TCP",
        "vless://u2@2.2.2.2:443?encryption=none#user_123",
        "vless://u3@3.3.3.3:443?encryption=none#user_123%20%7C%2045GB%20remaining",
        "vless://u4@4.4.4.4:443?encryption=none#%F0%9F%94%A5%20Premium%20User",
        "vless://u5@5.5.5.5:443?encryption=none#account%40example",
        "vless://u6@6.6.6.6:443?encryption=none#%F0%9F%93%A1Status|ok",
    ]
    for link in links:
        assert is_importable_configuration_link(link)
    assert len(subscription_config_links(links)) == len(links)


def test_plain_metadata_not_imported():
    body = "This is not a config\n<html><body>fail</body></html>\n"
    assert decode_subscription_links(body) == []


async def _run():
    test_remark_variants_imported()
    test_plain_metadata_not_imported()


if __name__ == "__main__":
    asyncio.run(_run())
