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

    class PluginManifest:
        # kwargs-tolerant like the real pydantic model — new cosmetic manifest
        # fields (shown_name, icon, image, ...) must not break the test stub.
        def __init__(self, **kw: Any) -> None:
            for k, v in kw.items():
                setattr(self, k, v)
            self.name = kw.get("name", "")
            self.version = kw.get("version", "")
            self.description = kw.get("description", "")
            self.category = kw.get("category", "")
            self.depends_on = kw.get("depends_on", [])
            self.routes_module = kw.get("routes_module")
            self.settings_tabs = kw.get("settings_tabs", [])
            self.interfaces = kw.get("interfaces", {})
            self.tools = kw.get("tools", [])

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

    async def get_current_user():  # route tests: auth always passes
        return {"sub": "owner"}

    mod.get_current_user = get_current_user
    mod.ToolDef = ToolDef
    mod.SettingsTab = SettingsTab
    mod.SkillDef = SkillDef
    mod.CredentialSlot = CredentialSlot
    mod.PluginManifest = PluginManifest
    mod.PluginContext = PluginContext
    mod.LunaPlugin = LunaPlugin
    sys.modules["luna_sdk"] = mod


_install_luna_sdk_stub()
