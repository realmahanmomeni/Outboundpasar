"""Rebecca-style subscriptions often serve base64 at the root URL (no /links suffix)."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.oc_upstream_subscription import fetch_upstream_subscription_body


@pytest.mark.asyncio
async def test_fetch_links_falls_back_to_root_when_suffix_404():
    link = "vless://uuid@1.2.3.4:443?encryption=none#remark"
    root_body = base64.b64encode(link.encode()).decode()
    base = "https://music.example.com/sub/token"

    class _Resp:
        def __init__(self, status: int, body: bytes = b""):
            self.status = status
            self.headers = {}
            self.content = MagicMock()
            self.content.read = AsyncMock(side_effect=[body, b""])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    calls: list[str] = []

    def _get(url, **kwargs):
        calls.append(url)
        if url.endswith("/links"):
            return _Resp(404)
        if url == base:
            return _Resp(200, root_body.encode())
        return _Resp(404)

    session = MagicMock()
    session.get = _get
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "app.services.oc_upstream_subscription.create_outbound_http_session",
        return_value=session,
    ), patch(
        "app.services.oc_upstream_subscription._host_resolves_to_public",
        return_value=True,
    ):
        text = await fetch_upstream_subscription_body(base, "links")
    assert root_body in text or link in text
    assert calls[0].endswith("/links")
    assert calls[1] == base
