"""Manifest sanity — no luna_sdk/runtime needed, just the data contract."""

import tomllib
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "plugin_monday"


def _manifest() -> dict:
    return tomllib.loads((PKG / "luna-plugin.toml").read_text())


def test_identity():
    m = _manifest()
    assert m["name"] == "plugin-monday"
    assert m["entry"] == "plugin_monday"
    assert m["sdk_version"] == "0"


def test_tool_count_matches_requires():
    m = _manifest()
    assert len(m["tools"]) == m["requires"]["tools"] == 17


def test_declares_oauth_env():
    m = _manifest()
    assert set(m["requires"]["env"]) == {"LUNA_MONDAY_CLIENT_ID", "LUNA_MONDAY_CLIENT_SECRET"}


def test_no_core_imports_in_source():
    for py in PKG.rglob("*.py"):
        for line in py.read_text().splitlines():
            s = line.strip()
            if s.startswith(("import luna", "from luna")) and "luna_sdk" not in s:
                raise AssertionError(f"{py.name}: forbidden core import: {s}")


def test_ships_iframe_settings_page():
    assert (PKG / "interface" / "webui" / "settings" / "index.html").exists()
