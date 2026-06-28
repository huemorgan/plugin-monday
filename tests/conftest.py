"""Test-only stub for `luna_sdk` so the package imports without a full Luna."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any


def _install_luna_sdk_stub() -> None:
    if "luna_sdk" in sys.modules:
        return

    mod = types.ModuleType("luna_sdk")

    @dataclass
    class ToolDef:
        name: str
        description: str = ""
        parameters: dict | None = None
        policy: str = "ask"
        risk_level: str = "low"
        timeout_seconds: int | None = None
        sensitive_args: list = field(default_factory=list)
        skill_gated: bool = False

    @dataclass
    class SettingsTab:
        id: str
        label: str
        icon: str = ""
        sort_order: int = 0
        iframe_src: str = ""

    @dataclass
    class SkillDef:
        name: str
        description: str = ""
        body: str = ""
        tools: list = field(default_factory=list)

    @dataclass
    class CredentialSlot:
        slug: str
        credential_name: str
        owner: str
        env_key_var: str | None = None
        env_base_url_var: str | None = None

    @dataclass
    class PluginManifest:
        name: str
        version: str
        description: str = ""
        category: str = ""
        depends_on: list = field(default_factory=list)
        routes_module: str | None = None
        settings_tabs: list = field(default_factory=list)
        interfaces: dict = field(default_factory=dict)
        tools: list = field(default_factory=list)

    class PluginContext:  # pragma: no cover - structural stand-in
        tool_registry: Any
        vault: Any
        skill_registry: Any

    class LunaPlugin:  # pragma: no cover - structural stand-in
        manifest: PluginManifest

        async def on_load(self, ctx: "PluginContext") -> None: ...

        async def on_unload(self) -> None: ...

        def credential_slots(self) -> list:
            return []

    mod.ToolDef = ToolDef
    mod.SettingsTab = SettingsTab
    mod.SkillDef = SkillDef
    mod.CredentialSlot = CredentialSlot
    mod.PluginManifest = PluginManifest
    mod.PluginContext = PluginContext
    mod.LunaPlugin = LunaPlugin
    sys.modules["luna_sdk"] = mod


_install_luna_sdk_stub()
