"""Cloud key-provisioning and transport contract for plugin-monday.

Direct transport (pasted token / gateway key) routes GraphQL through
`LUNA_MONDAY_BASE_URL` when set; the OAuth transport always talks to
monday's MCP passthrough host and is never proxied.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from plugin_monday import MondayPlugin
from plugin_monday.client import (
    API_URL,
    MCP_AUTHORIZE_URL,
    MCP_REGISTER_URL,
    MCP_TOKEN_URL,
    MondayClient,
)

PKG = Path(__file__).resolve().parents[1] / "plugin_monday"


def test_client_uses_base_url_override() -> None:
    c = MondayClient("tok", base_url="https://gw.example/proxy/monday")
    assert c._api_url == "https://gw.example/proxy/monday"
    assert c.transport == "direct"


def test_client_defaults_to_real_upstream() -> None:
    c = MondayClient("tok")
    assert c._api_url == API_URL
    assert c.transport == "direct"


def test_oauth_endpoints_are_monday_mcp_host() -> None:
    # No-app OAuth (DCR) lives on mcp.monday.com and is never proxied.
    assert MCP_REGISTER_URL == "https://mcp.monday.com/register"
    assert MCP_AUTHORIZE_URL == "https://mcp.monday.com/authorize"
    assert MCP_TOKEN_URL == "https://mcp.monday.com/token"


def test_oauth_client_uses_mcp_transport() -> None:
    c = MondayClient("tok", oauth={"client_id": "cid", "refresh_token": "r", "expires_at": None})
    assert c.transport == "oauth"


def test_credential_slot_advertises_base_url_var() -> None:
    slots = MondayPlugin().credential_slots()
    assert slots[0].slug == "monday"
    assert slots[0].env_key_var == "LUNA_MONDAY_API_KEY"
    assert slots[0].env_base_url_var == "LUNA_MONDAY_BASE_URL"


def test_manifest_and_code_versions_agree() -> None:
    toml_version = tomllib.loads((PKG / "luna-plugin.toml").read_text())["version"]
    code_version = re.search(r'version="([^"]+)"', (PKG / "__init__.py").read_text()).group(1)
    assert toml_version == code_version == MondayPlugin.manifest.version
