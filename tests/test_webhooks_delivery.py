"""Monday webhooks deliver through plugin-webhooks — gate + mint contract."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from plugin_monday import MondayPlugin, VAULT_WEBHOOK_SECRET_KEY


class _Cred:
    def __init__(self, value: str) -> None:
        self.value = value


class _Vault:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get_credential(self, key: str) -> _Cred:
        if key not in self.store:
            raise KeyError(key)
        return _Cred(self.store[key])

    async def store_credential(self, key: str, value: str, kind: str = "") -> None:
        self.store[key] = value


def _plugin_with_vault() -> tuple[MondayPlugin, _Vault]:
    plugin = MondayPlugin()
    vault = _Vault()
    plugin._ctx = types.SimpleNamespace(vault=vault)
    return plugin, vault


class _FakeWebhooks:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_hook(self, name, *, target=None, mode="sync", plugin=None, **kw):
        self.calls.append({"name": name, "target": target, "mode": mode, "plugin": plugin})
        return {
            "name": name,
            "hook_slug": "slug123",
            "public_url": f"https://luna.example/api/webhooks/hooks/agent/slug123",
        }


@pytest.fixture
def fake_webhooks(monkeypatch):
    fake = _FakeWebhooks()
    pkg = types.ModuleType("plugin_webhooks")
    state = types.ModuleType("plugin_webhooks.state")
    state.get_plugin = lambda: fake
    pkg.state = state
    monkeypatch.setitem(sys.modules, "plugin_webhooks", pkg)
    monkeypatch.setitem(sys.modules, "plugin_webhooks.state", state)
    return fake


@pytest.fixture
def no_webhooks(monkeypatch):
    monkeypatch.delitem(sys.modules, "plugin_webhooks", raising=False)
    monkeypatch.delitem(sys.modules, "plugin_webhooks.state", raising=False)


def test_url_requires_webhooks_plugin(no_webhooks) -> None:
    plugin, _ = _plugin_with_vault()
    with pytest.raises(RuntimeError) as e:
        asyncio.run(plugin._webhook_url())
    msg = str(e.value)
    assert "plugin-webhooks" in msg
    assert "Marketplace" in msg


def test_url_requires_loaded_instance(monkeypatch) -> None:
    # Package importable but plugin unloaded (get_plugin() → None) → same steer.
    pkg = types.ModuleType("plugin_webhooks")
    state = types.ModuleType("plugin_webhooks.state")
    state.get_plugin = lambda: None
    pkg.state = state
    monkeypatch.setitem(sys.modules, "plugin_webhooks", pkg)
    monkeypatch.setitem(sys.modules, "plugin_webhooks.state", state)

    plugin, _ = _plugin_with_vault()
    with pytest.raises(RuntimeError, match="plugin-webhooks"):
        asyncio.run(plugin._webhook_url())


def test_url_minted_through_webhooks_plugin(fake_webhooks) -> None:
    plugin, vault = _plugin_with_vault()
    url = asyncio.run(plugin._webhook_url())

    assert url == "https://luna.example/api/webhooks/hooks/agent/slug123"
    assert len(fake_webhooks.calls) == 1
    call = fake_webhooks.calls[0]
    assert call["name"] == "monday-events"
    assert call["plugin"] == "plugin-monday"
    # Sync is load-bearing: the gateway's queue path can't echo Monday's
    # registration challenge.
    assert call["mode"] == "sync"
    secret = vault.store[VAULT_WEBHOOK_SECRET_KEY]
    assert call["target"] == f"/api/p/plugin-monday/webhook/{secret}"


def test_secret_reused_across_mints(fake_webhooks) -> None:
    plugin, vault = _plugin_with_vault()
    asyncio.run(plugin._webhook_url())
    first = vault.store[VAULT_WEBHOOK_SECRET_KEY]
    asyncio.run(plugin._webhook_url())
    assert vault.store[VAULT_WEBHOOK_SECRET_KEY] == first
    assert fake_webhooks.calls[0]["target"] == fake_webhooks.calls[1]["target"]


def test_status_webhooks_ready_flag(monkeypatch, fake_webhooks) -> None:
    from plugin_monday.routes import _webhooks_ready

    assert _webhooks_ready() is True
    monkeypatch.delitem(sys.modules, "plugin_webhooks", raising=False)
    monkeypatch.delitem(sys.modules, "plugin_webhooks.state", raising=False)
    assert _webhooks_ready() is False
