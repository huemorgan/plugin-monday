"""Process-level holder for the live MondayClient.

008.5/phase07: decoupled from `get_plugin_registry()`. The OAuth callback and
disconnect routes used to reach into the registered plugin instance to swap its
`_client`. Instead the client lives here as a module singleton shared by
`on_load` and the routes via relative import — no core loader coupling, works
the same from a managed dir.
"""

from __future__ import annotations

from typing import Any

from .client import MondayClient

_client: MondayClient | None = None
# The live MondayPlugin — routes reach it for the plugin-webhooks mint
# (agent callback URL) without touching the core loader.
_plugin: Any | None = None


def get_client() -> MondayClient | None:
    return _client


def set_client(client: MondayClient | None) -> None:
    global _client
    _client = client


def get_plugin() -> Any | None:
    return _plugin


def set_plugin(plugin: Any | None) -> None:
    global _plugin
    _plugin = plugin
