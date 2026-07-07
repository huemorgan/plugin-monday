"""Personal-API-token connect — the no-OAuth-app path.

POST /connect-token validates the pasted token against monday (get_account),
stores it in the vault, and swaps the live client. A rejected token stores
nothing and returns 401.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugin_monday import routes as routes_mod
from plugin_monday.state import get_client, set_client


class FakeVault:
    def __init__(self):
        self.stored: dict[str, str] = {}

    async def store_credential(self, key, value, kind=""):
        self.stored[key] = value

    async def get_credential(self, key):
        if key not in self.stored:
            raise KeyError(key)

        class Cred:
            value = self.stored[key]

        return Cred()

    async def delete_credential(self, key):
        self.stored.pop(key)


class FakeEvents:
    def __init__(self):
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, name, payload):
        self.emitted.append((name, payload))


class FakeCtx:
    def __init__(self):
        self.vault = FakeVault()
        self.events = FakeEvents()


class FakeMonday:
    reject = False
    instances: list["FakeMonday"] = []

    def __init__(self, token, base_url=None):
        self.token = token
        self.closed = False
        FakeMonday.instances.append(self)

    async def get_account(self):
        if FakeMonday.reject:
            raise RuntimeError("HTTP 401")
        return {"me": {"account": {"name": "Acme", "id": 42}}}

    async def close(self):
        self.closed = True


@pytest.fixture()
def ctx():
    return FakeCtx()


@pytest.fixture()
def client(ctx, monkeypatch):
    from plugin_monday import client as client_mod

    monkeypatch.setattr(client_mod, "MondayClient", FakeMonday)
    FakeMonday.reject = False
    FakeMonday.instances = []
    set_client(None)
    app = FastAPI()
    routes_mod.register_routes(app, ctx)
    with TestClient(app) as c:
        yield c
    set_client(None)


def test_connect_token_stores_and_connects(client, ctx):
    resp = client.post("/api/p/plugin-monday/connect-token", json={"token": "tok_abc"})
    assert resp.status_code == 200
    assert resp.json() == {"connected": True, "account_name": "Acme"}
    assert ctx.vault.stored[routes_mod.VAULT_TOKEN_KEY] == "tok_abc"
    assert ctx.vault.stored[routes_mod.VAULT_ACCOUNT_KEY] == "42"
    assert isinstance(get_client(), FakeMonday)
    assert ctx.events.emitted == [
        ("monday.connected", {"account_name": "Acme", "account_id": "42"})
    ]


def test_connect_token_rejected_stores_nothing(client, ctx):
    FakeMonday.reject = True
    resp = client.post("/api/p/plugin-monday/connect-token", json={"token": "bad"})
    assert resp.status_code == 401
    assert routes_mod.VAULT_TOKEN_KEY not in ctx.vault.stored
    assert get_client() is None
    assert FakeMonday.instances[-1].closed  # probe client not leaked


def test_connect_token_blank_is_400(client, ctx):
    resp = client.post("/api/p/plugin-monday/connect-token", json={"token": "   "})
    assert resp.status_code == 400
    assert routes_mod.VAULT_TOKEN_KEY not in ctx.vault.stored


def test_settings_ui_offers_token_paste(client):
    html = client.get("/api/p/plugin-monday/ui/settings/").text
    assert "monday-token-input" in html
    assert "/connect-token" in html
