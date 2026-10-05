"""Focused tests: OC host name vs subscription display template."""
from __future__ import annotations

import asyncio

from app.services.oc_host_display import remark_looks_like_user_template
from app.services.oc_host_display import DEFAULT_OC_HOST_DISPLAY_TEMPLATE


def test_template_markers():
    assert remark_looks_like_user_template(DEFAULT_OC_HOST_DISPLAY_TEMPLATE)
    assert not remark_looks_like_user_template("VLESS TCP")
    assert not remark_looks_like_user_template("Shadowsocks TCP")


if __name__ == "__main__":
    test_template_markers()
    print("test_oc_host_display_separation: OK")
