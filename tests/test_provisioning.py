"""Cloud key-provisioning contract for plugin-monday.

GraphQL data calls route through `LUNA_MONDAY_BASE_URL` when set; the OAuth
token exchange stays on the real auth host (not proxied).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from plugin_monday import MondayPlugin
from plugin_monday.client import API_URL, AUTH_URL, MondayClient

PKG = Path(__file__).resolve().parents[1] / "plugin_monday"


def test_client_uses_base_url_override() -> None:
    c = MondayClient("tok", base_url="https://gw.example/proxy/monday")
    assert c._api_url == "https://gw.example/proxy/monday"


def test_client_defaults_to_real_upstream() -> None:
    c = MondayClient("tok")
    assert c._api_url == API_URL


def test_oauth_host_is_not_proxied() -> None:
    # The OAuth token exchange must always hit the real Monday auth host.
    assert AUTH_URL == "https://auth.monday.com/oauth2/token"


def test_credential_slot_advertises_base_url_var() -> None:
    slots = MondayPlugin().credential_slots()
    assert slots[0].slug == "monday"
    assert slots[0].env_key_var == "LUNA_MONDAY_API_KEY"
    assert slots[0].env_base_url_var == "LUNA_MONDAY_BASE_URL"


def test_manifest_and_code_versions_agree() -> None:
    toml_version = tomllib.loads((PKG / "luna-plugin.toml").read_text())["version"]
    code_version = re.search(r'version="([^"]+)"', (PKG / "__init__.py").read_text()).group(1)
    assert toml_version == code_version == MondayPlugin.manifest.version
