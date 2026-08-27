"""Trigger discovery + payload flattening — the playbook-facing contract.

MondayTriggerSource advertises every monday.* event routes.py can emit, and
the webhook route flattens boardId/itemId/type to the payload top level so
playbook filters like {"boardId": 123} match (the filter matcher does flat
dot-path equality).
"""

from __future__ import annotations

import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugin_monday import routes as routes_mod
from plugin_monday.triggers import MondayTriggerSource


class FakeVault:
    def __init__(self):
        self.stored = {routes_mod.VAULT_WEBHOOK_SECRET_KEY: "s3cret"}

    async def get_credential(self, key):
        if key not in self.stored:
            raise KeyError(key)

        class Cred:
            value = self.stored[key]

        return Cred()

    async def store_credential(self, key, value, kind=""):
        self.stored[key] = value


class FakeEvents:
    def __init__(self):
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, name, payload):
        self.emitted.append((name, payload))


class FakeCtx:
    def __init__(self):
        self.vault = FakeVault()
        self.events = FakeEvents()


@pytest.fixture()
def ctx():
    return FakeCtx()


@pytest.fixture()
def client(ctx):
    app = FastAPI()
    routes_mod.register_routes(app, ctx)
    with TestClient(app) as c:
        yield c


def test_trigger_source_covers_every_emitted_event():
    infos = asyncio.run(MondayTriggerSource().list_triggers())
    advertised = {i.event_pattern for i in infos}
    emitted = set(routes_mod.WEBHOOK_EVENT_MAP.values()) | {"monday.event"}
    assert emitted <= advertised
    assert all(i.app == "monday" and i.source == "monday" for i in infos)


def test_trigger_source_app_filter():
    assert asyncio.run(MondayTriggerSource().list_triggers("gmail")) == []
    assert len(asyncio.run(MondayTriggerSource().list_triggers("monday"))) > 0


def test_webhook_payload_flattened_for_filters(client, ctx):
    body = {
        "event": {
            "type": "create_pulse",
            "boardId": 5099347389,
            "pulseId": 111,
            "groupId": "topics",
            "userId": 7,
        }
    }
    resp = client.post("/api/p/plugin-monday/webhook/s3cret", json=body)
    assert resp.status_code == 200
    [(name, payload)] = ctx.events.emitted
    assert name == "monday.item.created"
    # Top-level keys a playbook filter can match on.
    assert payload["boardId"] == 5099347389
    assert payload["itemId"] == 111
    assert payload["type"] == "create_pulse"
    # Raw event preserved.
    assert payload["event"]["pulseId"] == 111


def test_webhook_unmapped_type_still_flattened(client, ctx):
    body = {"event": {"type": "some_future_event", "boardId": 42}}
    resp = client.post("/api/p/plugin-monday/webhook/s3cret", json=body)
    assert resp.status_code == 200
    [(name, payload)] = ctx.events.emitted
    assert name == "monday.event"
    assert payload["boardId"] == 42


def test_webhook_challenge_still_echoed(client, ctx):
    resp = client.post("/api/p/plugin-monday/webhook/s3cret", json={"challenge": "abc"})
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc"}
    assert ctx.events.emitted == []
