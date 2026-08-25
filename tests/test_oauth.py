"""No-app OAuth (Dynamic Client Registration) contract.

The connect flow self-registers a public client with monday (no app, no
client secret), runs authorization-code + PKCE + state, and the resulting
tokens drive GraphQL through the MCP passthrough tools (`all_api_read` /
`all_api_write`) — DCR tokens are rejected by api.monday.com/v2.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import httpx
import pytest

from plugin_monday import client as client_mod
from plugin_monday.client import (
    MCP_AUTHORIZE_URL,
    MCP_RPC_URL,
    MCP_TOKEN_URL,
    MondayClient,
    dcr_exchange_code,
    dcr_refresh,
)


def _patched_transport(handler):
    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *a, **kw):
        kw["transport"] = transport
        orig_init(self, *a, **kw)

    return patched_init, orig_init


def _run_with_transport(handler, coro_factory):
    patched, orig = _patched_transport(handler)
    httpx.AsyncClient.__init__ = patched
    try:
        return asyncio.run(coro_factory())
    finally:
        httpx.AsyncClient.__init__ = orig


# ── token endpoint contracts ──────────────────────────────────


def test_exchange_is_pkce_form_no_secret() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["form"] = dict(urllib.parse.parse_qsl(request.content.decode()))
        return httpx.Response(200, json={"access_token": "tok", "refresh_token": "ref"})

    result = _run_with_transport(
        handler, lambda: dcr_exchange_code("cid", "code123", "https://luna.example/cb", "verif"),
    )
    assert result["access_token"] == "tok"
    assert captured["url"] == MCP_TOKEN_URL
    assert captured["form"] == {
        "grant_type": "authorization_code",
        "code": "code123",
        "redirect_uri": "https://luna.example/cb",
        "client_id": "cid",
        "code_verifier": "verif",
    }
    assert "client_secret" not in captured["form"]


def test_refresh_grant() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["form"] = dict(urllib.parse.parse_qsl(request.content.decode()))
        return httpx.Response(200, json={"access_token": "tok2", "expires_in": 604800})

    _run_with_transport(handler, lambda: dcr_refresh("cid", "ref"))
    assert captured["form"] == {
        "grant_type": "refresh_token",
        "refresh_token": "ref",
        "client_id": "cid",
    }


# ── MCP passthrough transport ─────────────────────────────────


def _mcp_client() -> MondayClient:
    return MondayClient(
        "acc", oauth={"client_id": "cid", "refresh_token": "ref", "expires_at": None},
    )


def test_query_routes_to_all_api_read_with_string_variables() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1,
            "result": {"structuredContent": {"boards": [{"id": "1"}]}},
        })

    async def run():
        c = _mcp_client()
        try:
            return await c.list_boards(limit=5)
        finally:
            await c.close()

    boards = _run_with_transport(handler, run)
    assert boards == [{"id": "1"}]
    assert captured["url"] == MCP_RPC_URL
    assert captured["auth"] == "Bearer acc"
    params = captured["body"]["params"]
    assert params["name"] == "all_api_read"
    # The passthrough tools require variables as a JSON *string*.
    assert isinstance(params["arguments"]["variables"], str)
    assert json.loads(params["arguments"]["variables"]) == {"limit": 5}


def test_mutation_routes_to_all_api_write() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1,
            "result": {"structuredContent": {"create_item": {"id": "9"}}},
        })

    async def run():
        c = _mcp_client()
        try:
            return await c.create_item(1, "hello")
        finally:
            await c.close()

    item = _run_with_transport(handler, run)
    assert item == {"id": "9"}
    assert captured["body"]["params"]["name"] == "all_api_write"


def test_tool_error_raises_monday_api_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1,
            "result": {"isError": True, "content": [{"type": "text", "text": "boom"}]},
        })

    async def run():
        c = _mcp_client()
        try:
            await c.get_account()
        finally:
            await c.close()

    with pytest.raises(client_mod.MondayAPIError, match="boom"):
        _run_with_transport(handler, run)


def test_401_triggers_refresh_and_retry() -> None:
    calls = {"rpc": 0, "token": 0}
    refreshed = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == MCP_TOKEN_URL:
            calls["token"] += 1
            return httpx.Response(200, json={
                "access_token": "acc2", "refresh_token": "ref2", "expires_in": 604800,
            })
        calls["rpc"] += 1
        if request.headers.get("Authorization") != "Bearer acc2":
            return httpx.Response(401)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1,
            "result": {"structuredContent": {"me": {"name": "roy"}}},
        })

    async def run():
        async def on_refresh(bundle):
            refreshed.append(bundle)

        c = MondayClient(
            "expired",
            oauth={"client_id": "cid", "refresh_token": "ref", "expires_at": None},
            on_refresh=on_refresh,
        )
        try:
            return await c.get_account()
        finally:
            await c.close()

    data = _run_with_transport(handler, run)
    assert data == {"me": {"name": "roy"}}
    assert calls == {"rpc": 2, "token": 1}
    assert refreshed and refreshed[0]["access_token"] == "acc2"
    assert refreshed[0]["refresh_token"] == "ref2"


# ── connect/callback routes ───────────────────────────────────

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from plugin_monday import routes as routes_mod  # noqa: E402
from plugin_monday.state import get_client, set_client  # noqa: E402
from test_connect_token import FakeCtx  # noqa: E402


class FakeOAuthMonday:
    def __init__(self):
        self.closed = False

    @property
    def transport(self):
        return "oauth"

    async def get_account(self):
        return {"me": {"account": {"name": "Acme", "id": 42}}}

    async def close(self):
        self.closed = True


@pytest.fixture()
def oauth_env(monkeypatch):
    ctx = FakeCtx()

    async def fake_register(redirect_uri, client_name="Luna"):
        return {"client_id": "dcr-cid", "redirect_uris": [redirect_uri]}

    exchanged = {}

    async def fake_exchange(client_id, code, redirect_uri, verifier):
        exchanged.update(client_id=client_id, code=code,
                         redirect_uri=redirect_uri, verifier=verifier)
        return {"access_token": "acc", "refresh_token": "ref", "expires_in": 604800}

    monkeypatch.setattr(client_mod, "dcr_register", fake_register)
    monkeypatch.setattr(client_mod, "dcr_exchange_code", fake_exchange)
    monkeypatch.setattr(
        client_mod, "client_from_bundle", lambda bundle, on_refresh=None: FakeOAuthMonday(),
    )
    set_client(None)
    app = FastAPI()
    routes_mod.register_routes(app, ctx)
    with TestClient(app) as http:
        yield http, ctx, exchanged
    set_client(None)


def test_connect_redirects_to_monday_with_pkce_and_state(oauth_env):
    http, ctx, _ = oauth_env
    resp = http.get(
        "/api/p/plugin-monday/connect",
        headers={"referer": "https://luna.example/api/p/plugin-monday/ui/settings/"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    url = urllib.parse.urlparse(resp.headers["location"])
    q = dict(urllib.parse.parse_qsl(url.query))
    assert resp.headers["location"].startswith(MCP_AUTHORIZE_URL)
    assert q["client_id"] == "dcr-cid"
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] and q["state"]
    assert q["redirect_uri"] == "https://luna.example/api/p/plugin-monday/callback"
    pending = json.loads(ctx.vault.stored[routes_mod.VAULT_OAUTH_PENDING_KEY])
    assert pending["state"] == q["state"]


def test_callback_exchanges_and_stores_bundle(oauth_env):
    http, ctx, exchanged = oauth_env
    http.get(
        "/api/p/plugin-monday/connect",
        headers={"referer": "https://luna.example/api/p/plugin-monday/ui/settings/"},
        follow_redirects=False,
    )
    state = json.loads(ctx.vault.stored[routes_mod.VAULT_OAUTH_PENDING_KEY])["state"]

    resp = http.get(f"/api/p/plugin-monday/callback?code=abc&state={state}")
    assert resp.status_code == 200

    assert exchanged["client_id"] == "dcr-cid"
    assert exchanged["code"] == "abc"
    bundle = json.loads(ctx.vault.stored[routes_mod.VAULT_OAUTH_BUNDLE_KEY])
    assert bundle["access_token"] == "acc"
    assert bundle["refresh_token"] == "ref"
    assert bundle["public_base"] == "https://luna.example"
    assert routes_mod.VAULT_OAUTH_PENDING_KEY not in ctx.vault.stored
    assert isinstance(get_client(), FakeOAuthMonday)
    assert ("monday.connected", {"account_name": "Acme", "account_id": "42"}) in ctx.events.emitted


def test_callback_rejects_bad_state(oauth_env):
    http, ctx, _ = oauth_env
    http.get(
        "/api/p/plugin-monday/connect",
        headers={"referer": "https://luna.example/api/p/plugin-monday/ui/settings/"},
        follow_redirects=False,
    )
    resp = http.get("/api/p/plugin-monday/callback?code=abc&state=WRONG")
    assert resp.status_code == 400
    assert routes_mod.VAULT_OAUTH_BUNDLE_KEY not in ctx.vault.stored
    assert get_client() is None
