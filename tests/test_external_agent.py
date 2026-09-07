"""Luna listed as an external agent inside monday.com.

Covers the helper contract (signature, prompt, SSE framing, dedupe), the
client mutations (API-Version: dev, ≥40 s connect timeout), the settings
endpoints (connect → mint URL → create → activate → store once-only secrets;
disconnect), and the callback route (signed POST; chat streams SSE, mention
acks then posts an update in the thread as the agent).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import time
import types

import httpx
import pytest

from plugin_monday import agent as agent_mod
from plugin_monday.client import AGENT_API_VERSION, MondayClient

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from plugin_monday import MondayPlugin  # noqa: E402
from plugin_monday import routes as routes_mod  # noqa: E402
from plugin_monday.state import set_client, set_plugin  # noqa: E402


# ── helpers ───────────────────────────────────────────────────


def _sign(secret: str, ts: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()


def test_signature_roundtrip() -> None:
    raw = b'{"event":"agent_triggered"}'
    ts = "1782326623754"
    good = _sign("sec", ts, raw)
    assert agent_mod.verify_signature("sec", ts, raw, good)
    assert not agent_mod.verify_signature("sec", ts, raw + b" ", good)
    assert not agent_mod.verify_signature("other", ts, raw, good)
    assert not agent_mod.verify_signature("sec", "1", raw, good)
    assert not agent_mod.verify_signature("", ts, raw, good)


def test_timestamp_window() -> None:
    now = 1_800_000_000.0
    assert agent_mod.timestamp_fresh(str(int(now * 1000)), now=now)
    assert agent_mod.timestamp_fresh(str(int((now - 500) * 1000)), now=now)
    assert not agent_mod.timestamp_fresh(str(int((now - 3600) * 1000)), now=now)
    assert not agent_mod.timestamp_fresh("nope", now=now)


def test_prompt_prefers_user_words() -> None:
    body = {
        "triggerType": "mention",
        "payload": {"text": "Instruction prompt", "updateBody": "hey luna what's up",
                    "itemId": 5, "boardId": 9, "updateId": 77},
    }
    p = agent_mod.build_prompt(body)
    assert "@mentioned" in p
    assert p.index("hey luna what's up") < p.index("Instruction prompt")
    assert "Item id: 5" in p and "Board id: 9" in p and "Update id: 77" in p
    assert "chat window" in agent_mod.build_prompt({"triggerType": "chat", "payload": {}})
    assert "assigned" in agent_mod.build_prompt({"triggerType": "assigned", "payload": {}})


def test_sse_framing_and_turn_text() -> None:
    assert agent_mod.sse_text("hi") == b'data: {"type": "text", "content": "hi"}\n\n'
    assert agent_mod.SSE_DONE == b"data: [DONE]\n\n"
    assert agent_mod.turn_text("  reply ") == "reply"
    assert agent_mod.turn_text({"error": "boom"}) == ""
    assert agent_mod.turn_text({"_aborted": "timeout", "error": "x"}) == ""
    assert agent_mod.turn_text({"_raw": "text"}) == "text"


def test_delta_text_reads_pydantic_ai_events() -> None:
    start = types.SimpleNamespace(event_kind="part_start",
                                  part=types.SimpleNamespace(part_kind="text", content="He"))
    delta = types.SimpleNamespace(event_kind="part_delta",
                                  delta=types.SimpleNamespace(part_delta_kind="text", content_delta="llo"))
    tool = types.SimpleNamespace(event_kind="function_tool_call", part=None)
    assert agent_mod.delta_text(start) == "He"
    assert agent_mod.delta_text(delta) == "llo"
    assert agent_mod.delta_text(tool) is None


def test_dedupe_keys() -> None:
    r = agent_mod.RecentKeys(limit=2)
    h = {"x-monday-timestamp": "1"}
    b = {"triggerType": "mention", "payload": {"updateId": 5, "itemId": 1}}
    k = agent_mod.dedupe_key(h, b)
    assert not r.seen(k)
    assert r.seen(k)
    r.seen("a"); r.seen("b")
    assert not r.seen(k)  # evicted


# ── client mutations ──────────────────────────────────────────


def _direct_client(handler) -> MondayClient:
    c = MondayClient("tok")
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def test_connect_uses_dev_version_and_long_timeout() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"data": {"connect_external_agent_sync": {
            "agent_id": 139988, "signing_secret": "ss", "api_token": "at", "instructions": None}}})

    c = _direct_client(handler)
    out = asyncio.run(c.connect_external_agent("Luna", "https://luna.example/hook"))
    assert out["agent_id"] == 139988 and out["api_token"] == "at"
    assert seen["headers"]["api-version"] == AGENT_API_VERSION == "dev"
    assert seen["body"]["variables"]["input"]["custom"] == {
        "name": "Luna", "callback_url": "https://luna.example/hook"}
    assert seen["timeout"]["read"] >= 40


def test_agent_mutations_shapes() -> None:
    queries = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        queries.append((body["query"], body.get("variables"), request.headers.get("api-version")))
        return httpx.Response(200, json={"data": {
            "activate_agent": {"success": True},
            "disconnect_external_agent": {"success": True},
            "add_agent_resource_access": {"success": True},
            "update_custom_agent": {"success": True, "signing_secret": None},
            "create_update": {"id": "1"},
        }})

    c = _direct_client(handler)
    assert asyncio.run(c.activate_agent(1))["success"] is True
    assert asyncio.run(c.disconnect_external_agent(1))["success"] is True
    assert asyncio.run(c.add_agent_resource_access(1, 2))["success"] is True
    assert asyncio.run(c.update_custom_agent(1, name="Lu"))["success"] is True
    asyncio.run(c.create_update(3, "hi", parent_id=9))
    assert all(v == "dev" for _, _, v in queries[:4])
    assert queries[2][1] == {"id": "1", "res": "2", "scope": "BOARD", "perm": "READ_WRITE"}
    assert "parent_id:$parent" in queries[4][0] and queries[4][1]["parent"] == "9"
    assert queries[4][2] is None  # regular calls stay on the stable version


# ── routes ────────────────────────────────────────────────────


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
        self.emitted = []

    async def emit(self, name, payload):
        self.emitted.append((name, payload))


class FakeAgent:
    """Stand-in for ctx.agent — streams two deltas then returns the text."""

    def __init__(self, text="Hello from Luna", stream=True, fail=False):
        self.text, self.stream, self.fail = text, stream, fail
        self.prompts = []

    async def run_turn(self, prompt, **kw):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("model down")
        handler = kw.get("event_stream_handler")
        if handler and self.stream:
            async def events():
                yield types.SimpleNamespace(event_kind="part_start",
                                            part=types.SimpleNamespace(part_kind="text", content="Hello "))
                yield types.SimpleNamespace(event_kind="part_delta",
                                            delta=types.SimpleNamespace(part_delta_kind="text", content_delta="from Luna"))
            await handler(None, events())
        return self.text, None


class FakeCtx:
    def __init__(self):
        self.vault = FakeVault()
        self.events = FakeEvents()
        self.agent = FakeAgent()


class FakeMonday:
    transport = "direct"

    def __init__(self):
        self.calls = []
        self.fail_connect = None

    async def connect_external_agent(self, name, callback_url):
        self.calls.append(("connect", name, callback_url))
        if self.fail_connect:
            raise self.fail_connect
        return {"agent_id": 139988, "signing_secret": "ss", "api_token": "at", "instructions": "x"}

    async def activate_agent(self, agent_id):
        self.calls.append(("activate", str(agent_id)))
        return {"success": True}

    async def disconnect_external_agent(self, agent_id):
        self.calls.append(("disconnect", str(agent_id)))
        return {"success": True}

    async def create_update(self, item_id, body, *, parent_id=None):
        self.calls.append(("owner_update", item_id, body, parent_id))
        return {"id": "u1"}

    async def get_account(self):
        return {"me": {"account": {"name": "Acme"}}}

    async def list_boards(self, limit=500):
        return []

    async def close(self):
        pass


class FakeWebhooks:
    def __init__(self):
        self.calls = []

    async def create_hook(self, name, *, target=None, mode="sync", plugin=None, **kw):
        self.calls.append({"name": name, "target": target, "mode": mode, "plugin": plugin})
        return {"name": name, "hook_slug": "h1",
                "public_url": "https://luna.com.ai/api/webhooks/hooks/acme/h1"}


@pytest.fixture
def env(monkeypatch):
    fake = FakeWebhooks()
    pkg = types.ModuleType("plugin_webhooks")
    state = types.ModuleType("plugin_webhooks.state")
    state.get_plugin = lambda: fake
    pkg.state = state
    monkeypatch.setitem(sys.modules, "plugin_webhooks", pkg)
    monkeypatch.setitem(sys.modules, "plugin_webhooks.state", state)

    ctx = FakeCtx()
    ctx.vault.stored[routes_mod.VAULT_TOKEN_KEY] = "tok"  # connected via personal token
    plugin = MondayPlugin()
    plugin._ctx = ctx
    set_plugin(plugin)
    monday = FakeMonday()
    set_client(monday)
    app = FastAPI()
    routes_mod.register_routes(app, ctx)
    with TestClient(app) as c:
        yield types.SimpleNamespace(client=c, ctx=ctx, monday=monday, webhooks=fake, plugin=plugin)
    set_client(None)
    set_plugin(None)


BASE = "/api/p/plugin-monday"


def _bundle(env) -> dict:
    return json.loads(env.ctx.vault.stored[agent_mod.VAULT_AGENT_BUNDLE_KEY])


def test_connect_mints_url_creates_activates_and_stores_secrets(env) -> None:
    r = env.client.post(f"{BASE}/agent/connect", json={"name": "Luna QA"})
    assert r.status_code == 200, r.text
    a = r.json()["agent"]
    assert a["agent_id"] == "139988" and a["name"] == "Luna QA" and a["active"] is True
    assert a["callback_url"] == "https://luna.com.ai/api/webhooks/hooks/acme/h1"
    assert "api_token" not in a and "signing_secret" not in a

    hook = env.webhooks.calls[0]
    secret = env.ctx.vault.stored[agent_mod.VAULT_AGENT_CALLBACK_SECRET_KEY]
    assert hook["name"] == agent_mod.HOOK_NAME and hook["mode"] == "sync"
    assert hook["target"] == f"/api/p/plugin-monday/agent/{secret}"
    assert hook["plugin"] == "plugin-monday"

    assert env.monday.calls[0] == ("connect", "Luna QA", a["callback_url"])
    assert env.monday.calls[1] == ("activate", "139988")
    b = _bundle(env)
    assert b["signing_secret"] == "ss" and b["api_token"] == "at" and b["active"] is True
    assert env.ctx.events.emitted[0][0] == "monday.agent.connected"

    # status carries the public view; a second connect is refused
    s = env.client.get(f"{BASE}/status").json()
    assert s["agent"]["agent_id"] == "139988"
    assert env.client.post(f"{BASE}/agent/connect", json={}).status_code == 409


def test_connect_requires_webhooks_plugin(env, monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "plugin_webhooks")
    monkeypatch.delitem(sys.modules, "plugin_webhooks.state")
    r = env.client.post(f"{BASE}/agent/connect", json={})
    assert r.status_code == 409
    assert "plugin-webhooks" in r.text
    assert env.monday.calls == []


def test_connect_steers_oauth_transport_to_token(env) -> None:
    env.monday.transport = "oauth"
    env.monday.fail_connect = RuntimeError("Cannot query field connect_external_agent_sync")
    r = env.client.post(f"{BASE}/agent/connect", json={})
    assert r.status_code == 502
    assert "personal API token" in r.text
    assert agent_mod.VAULT_AGENT_BUNDLE_KEY not in env.ctx.vault.stored


def test_disconnect_removes_remote_and_local(env) -> None:
    env.client.post(f"{BASE}/agent/connect", json={})
    r = env.client.post(f"{BASE}/agent/disconnect")
    assert r.status_code == 200 and r.json()["removed_in_monday"] is True
    assert ("disconnect", "139988") in env.monday.calls
    assert agent_mod.VAULT_AGENT_BUNDLE_KEY not in env.ctx.vault.stored
    assert env.client.get(f"{BASE}/status").json()["agent"] is None


def _post_callback(env, body: dict, *, secret=None, signing="ss", ts=None):
    env.client.post(f"{BASE}/agent/connect", json={}) if agent_mod.VAULT_AGENT_BUNDLE_KEY not in env.ctx.vault.stored else None
    path_secret = secret or env.ctx.vault.stored[agent_mod.VAULT_AGENT_CALLBACK_SECRET_KEY]
    raw = json.dumps(body).encode()
    ts = ts or str(int(time.time() * 1000))
    return env.client.post(
        f"{BASE}/agent/{path_secret}", content=raw,
        headers={"content-type": "application/json", "x-monday-agent-id": "139988",
                 "x-monday-timestamp": ts, "x-monday-signature": _sign(signing, ts, raw)},
    )


def _chat(text="what's on my board?", stream=True) -> dict:
    return {"event": "agent_triggered", "triggerType": "chat",
            "payload": {"text": text, "itemId": None, "boardId": None},
            "timestamp": "2026-09-07T10:00:00Z", "stream": stream}


def test_callback_rejects_bad_signature_secret_and_stale(env) -> None:
    assert _post_callback(env, _chat(), signing="wrong").status_code == 403
    assert _post_callback(env, _chat(), secret="nope").status_code == 403
    assert _post_callback(env, _chat(), ts="1000").status_code == 403
    assert env.ctx.agent.prompts == []


def test_chat_streams_sse_reply(env) -> None:
    r = _post_callback(env, _chat())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    lines = [l for l in r.text.split("\n\n") if l.startswith("data:")]
    chunks = [json.loads(l[len("data: "):]) for l in lines[:-1]]
    assert [c["content"] for c in chunks] == ["Hello ", "from Luna"]
    assert all(c["type"] == "text" for c in chunks)
    assert lines[-1] == "data: [DONE]"
    assert "chat window" in env.ctx.agent.prompts[0]
    assert "what's on my board?" in env.ctx.agent.prompts[0]


def test_chat_without_deltas_sends_final_text(env) -> None:
    env.ctx.agent.stream = False
    r = _post_callback(env, _chat())
    lines = [l for l in r.text.split("\n\n") if l.startswith("data:")]
    assert json.loads(lines[0][6:])["content"] == "Hello from Luna"
    assert lines[-1] == "data: [DONE]"


def test_chat_turn_error_still_closes_stream(env) -> None:
    env.ctx.agent.fail = True
    r = _post_callback(env, _chat())
    assert r.status_code == 200
    assert "model down" in r.text and r.text.rstrip().endswith("data: [DONE]")


def test_chat_non_streaming_json(env) -> None:
    r = _post_callback(env, _chat(stream=False))
    assert r.status_code == 200
    assert r.json() == {"message": "Hello from Luna"}


def test_mention_acks_then_replies_in_thread_as_agent(env, monkeypatch) -> None:
    posted = []

    class AgentClient:
        async def create_update(self, item_id, body, *, parent_id=None):
            posted.append((item_id, body, parent_id))
            return {"id": "u9"}

    async def fake_agent_client(bundle):
        assert bundle["api_token"] == "at"
        return AgentClient()

    # Route-local closure → patch through the module-level seam the route uses.
    monkeypatch.setattr(routes_mod, "_AgentIdentityClient", lambda c, v: AgentClient())
    body = {"event": "agent_triggered", "triggerType": "mention",
            "payload": {"text": "instr", "updateBody": "@Luna summarize", "itemId": 42,
                        "boardId": 7, "updateId": 500}, "stream": True}
    r = _post_callback(env, body)
    assert r.status_code == 200
    assert r.text == "data: [DONE]\n\n"
    # background task runs on the TestClient's loop; give it a tick
    for _ in range(50):
        if posted:
            break
        time.sleep(0.02)
    assert posted == [(42, "Hello from Luna", 500)]
    assert "@mentioned" in env.ctx.agent.prompts[0]
    assert "@Luna summarize" in env.ctx.agent.prompts[0]


def test_mention_retry_is_deduped(env, monkeypatch) -> None:
    monkeypatch.setattr(routes_mod, "_AgentIdentityClient", lambda c, v: FakeMonday())
    body = {"event": "agent_triggered", "triggerType": "assigned",
            "payload": {"text": "do it", "itemId": 1, "boardId": 2}, "stream": True}
    ts = str(int(time.time() * 1000))
    assert _post_callback(env, body, ts=ts).status_code == 200
    assert _post_callback(env, body, ts=ts).status_code == 200
    time.sleep(0.2)
    assert len(env.ctx.agent.prompts) == 1


def test_post_reply_falls_back_to_owner() -> None:
    class Bad:
        async def create_update(self, *a, **k):
            raise RuntimeError("no board access")

    owner = FakeMonday()
    res = asyncio.run(agent_mod.post_reply(
        text="hi", payload={"itemId": 3, "updateId": 8}, agent_client=Bad(), owner_client=owner,
    ))
    assert res["posted"] is True and res["as"] == "owner"
    assert owner.calls == [("owner_update", 3, "hi", 8)]


def test_grant_tool_requires_listed_agent(env) -> None:
    class Registry:
        def __init__(self):
            self.tools = {}

        def register(self, plugin, tool_def, handler, skill_gated=False):
            self.tools[tool_def.name] = (tool_def, handler)

    reg = Registry()
    env.plugin._register_tools(types.SimpleNamespace(tool_registry=reg))
    tool_def, handler = reg.tools["monday_agent_grant_board_access"]
    assert tool_def.policy == "ask"
    with pytest.raises(RuntimeError, match="Add to monday.com"):
        asyncio.run(handler(board_id=5))

    async def grant(agent_id, resource_id, *, scope_type, permission_type):
        env.monday.calls.append(("grant", str(agent_id), str(resource_id), scope_type, permission_type))
        return {"success": True}

    env.monday.add_agent_resource_access = grant
    env.client.post(f"{BASE}/agent/connect", json={})
    out = asyncio.run(handler(board_id=5, permission="read"))
    assert out["granted"] is True and out["permission"] == "READ"
    assert env.monday.calls[-1] == ("grant", "139988", "5", "BOARD", "READ")
