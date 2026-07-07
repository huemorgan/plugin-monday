"""OAuth token-exchange contract.

monday's token endpoint returns 401 if the authorize request carried a
redirect_uri and the token request omits it — exchange_code must repeat the
exact redirect_uri the /connect redirect used (persisted in the vault,
because the callback request's referer points at monday, not at Luna).
"""

from __future__ import annotations

import asyncio
import json

import httpx

from plugin_monday.client import AUTH_URL, exchange_code


def _run_exchange(**kwargs):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"access_token": "tok"})

    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *a, **kw):
        kw["transport"] = transport
        orig_init(self, *a, **kw)

    httpx.AsyncClient.__init__ = patched_init
    try:
        result = asyncio.run(exchange_code("cid", "secret", "code123", **kwargs))
    finally:
        httpx.AsyncClient.__init__ = orig_init
    return result, captured


def test_exchange_includes_redirect_uri_when_given() -> None:
    result, captured = _run_exchange(redirect_uri="https://luna.example/api/p/plugin-monday/callback")
    assert result == {"access_token": "tok"}
    assert captured["url"] == AUTH_URL
    assert captured["payload"] == {
        "client_id": "cid",
        "client_secret": "secret",
        "code": "code123",
        "redirect_uri": "https://luna.example/api/p/plugin-monday/callback",
    }


def test_exchange_omits_redirect_uri_when_absent() -> None:
    _, captured = _run_exchange()
    assert "redirect_uri" not in captured["payload"]
