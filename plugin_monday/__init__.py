"""plugin-monday — Monday.com board/item management via GraphQL.

Connects Luna to Monday.com via OAuth 2.0. All tools are skill-gated;
the agent loads monday-boards, monday-items, monday-columns, or
monday-updates skills to gain access.
"""

from __future__ import annotations

import logging
from typing import Any

from luna_sdk import (
    LunaPlugin,
    PluginContext,
    PluginManifest,
    SettingsTab,
    SkillDef,
    ToolDef,
)

from .client import MondayClient
from .state import get_client, set_client

log = logging.getLogger("plugin-monday")

VAULT_TOKEN_KEY = "plugin_monday.oauth"
VAULT_ACCOUNT_KEY = "plugin_monday.account_id"


class MondayPlugin(LunaPlugin):
    manifest = PluginManifest(
        name="plugin-monday",
        version="0.1.0",
        description="Monday.com board and item management via GraphQL.",
        category="connectors",
        depends_on=["plugin-vault"],
        routes_module="routes",
        settings_tabs=[
            SettingsTab(
                id="monday",
                label="Monday.com",
                icon="kanban",
                sort_order=65,
                iframe_src="/api/p/plugin-monday/ui/settings/",
            ),
        ],
        interfaces={"webui": "interface/webui"},
    )

    async def on_load(self, ctx: PluginContext) -> None:
        set_client(None)
        vault = ctx.vault
        if vault is None:
            log.warning("Vault not available; plugin-monday inactive")
            return

        token: str | None = None
        try:
            cred = await vault.get_credential(VAULT_TOKEN_KEY)
            token = cred.value
        except KeyError:
            pass

        if token:
            set_client(MondayClient(token))

        self._register_tools(ctx)
        self._register_skills(ctx)
        log.info("plugin-monday loaded (tools=17, connected=%s)", get_client() is not None)

    async def on_unload(self) -> None:
        client = get_client()
        if client is not None:
            await client.close()
            set_client(None)

    def _get_client(self) -> MondayClient:
        client = get_client()
        if client is None:
            raise RuntimeError(
                "Monday.com not connected. Ask the owner to connect in Settings > Monday.com."
            )
        return client

    # ── tools ─────────────────────────────────────────────────

    def _register_tools(self, ctx: PluginContext) -> None:
        plugin = self.manifest.name

        def _reg(tool_def: ToolDef, handler) -> None:
            ctx.tool_registry.register(plugin, tool_def, handler, skill_gated=True)

        # --- boards ---

        async def _list_boards(
            limit: int = 25, workspace_id: int | None = None,
        ) -> dict[str, Any]:
            return {"boards": await self._get_client().list_boards(
                limit=limit, workspace_id=workspace_id,
            )}

        _reg(
            ToolDef(
                name="monday_list_boards",
                description="List Monday.com boards. Optionally filter by workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max boards to return.", "default": 25},
                        "workspace_id": {"type": "integer", "description": "Filter to a specific workspace."},
                    },
                },
            ),
            _list_boards,
        )

        async def _get_board(board_id: int) -> dict[str, Any]:
            return await self._get_client().get_board(board_id)

        _reg(
            ToolDef(
                name="monday_get_board",
                description="Get details of a Monday.com board including columns and groups.",
                parameters={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "integer", "description": "The board ID."},
                    },
                    "required": ["board_id"],
                },
            ),
            _get_board,
        )

        async def _list_groups(board_id: int) -> dict[str, Any]:
            return {"groups": await self._get_client().list_groups(board_id)}

        _reg(
            ToolDef(
                name="monday_list_groups",
                description="List groups in a Monday.com board.",
                parameters={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "integer", "description": "The board ID."},
                    },
                    "required": ["board_id"],
                },
            ),
            _list_groups,
        )

        async def _create_group(board_id: int, group_name: str) -> dict[str, Any]:
            return await self._get_client().create_group(board_id, group_name)

        _reg(
            ToolDef(
                name="monday_create_group",
                description="Create a new group in a Monday.com board.",
                parameters={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "integer", "description": "The board ID."},
                        "group_name": {"type": "string", "description": "Name for the new group."},
                    },
                    "required": ["board_id", "group_name"],
                },
            ),
            _create_group,
        )

        # --- items ---

        async def _list_items(
            board_id: int,
            limit: int = 25,
            column_id: str | None = None,
            value: str | None = None,
        ) -> dict[str, Any]:
            return {"items": await self._get_client().list_items(
                board_id, limit=limit, column_id=column_id, value=value,
            )}

        _reg(
            ToolDef(
                name="monday_list_items",
                description="List items on a Monday.com board. Optionally filter by column value.",
                parameters={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "integer", "description": "The board ID."},
                        "limit": {"type": "integer", "description": "Max items to return.", "default": 25},
                        "column_id": {"type": "string", "description": "Column ID to filter by."},
                        "value": {"type": "string", "description": "Column value to match."},
                    },
                    "required": ["board_id"],
                },
            ),
            _list_items,
        )

        async def _get_item(item_id: int) -> dict[str, Any]:
            return await self._get_client().get_item(item_id)

        _reg(
            ToolDef(
                name="monday_get_item",
                description="Get details of a Monday.com item including column values and subitems.",
                parameters={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "The item ID."},
                    },
                    "required": ["item_id"],
                },
            ),
            _get_item,
        )

        async def _create_item(
            board_id: int,
            item_name: str,
            group_id: str | None = None,
            column_values: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return await self._get_client().create_item(
                board_id, item_name, group_id=group_id, column_values=column_values,
            )

        _reg(
            ToolDef(
                name="monday_create_item",
                description="Create a new item on a Monday.com board.",
                parameters={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "integer", "description": "The board ID."},
                        "group_id": {"type": "string", "description": "Target group ID (optional)."},
                        "item_name": {"type": "string", "description": "Name for the new item."},
                        "column_values": {"type": "object", "description": "Column values as JSON object."},
                    },
                    "required": ["board_id", "item_name"],
                },
            ),
            _create_item,
        )

        async def _update_item(
            item_id: int, board_id: int, column_values: dict[str, Any],
        ) -> dict[str, Any]:
            return await self._get_client().update_item(item_id, board_id, column_values)

        _reg(
            ToolDef(
                name="monday_update_item",
                description="Update column values on a Monday.com item.",
                parameters={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "The item ID."},
                        "board_id": {"type": "integer", "description": "The board ID."},
                        "column_values": {"type": "object", "description": "Column values to update as JSON object."},
                    },
                    "required": ["item_id", "board_id", "column_values"],
                },
            ),
            _update_item,
        )

        async def _delete_item(item_id: int) -> dict[str, Any]:
            return await self._get_client().delete_item(item_id)

        _reg(
            ToolDef(
                name="monday_delete_item",
                description="Delete a Monday.com item.",
                parameters={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "The item ID."},
                    },
                    "required": ["item_id"],
                },
                risk_level="high",
            ),
            _delete_item,
        )

        async def _move_item(item_id: int, group_id: str) -> dict[str, Any]:
            return await self._get_client().move_item(item_id, group_id)

        _reg(
            ToolDef(
                name="monday_move_item",
                description="Move a Monday.com item to a different group.",
                parameters={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "The item ID."},
                        "group_id": {"type": "string", "description": "Target group ID."},
                    },
                    "required": ["item_id", "group_id"],
                },
            ),
            _move_item,
        )

        async def _archive_item(item_id: int) -> dict[str, Any]:
            return await self._get_client().archive_item(item_id)

        _reg(
            ToolDef(
                name="monday_archive_item",
                description="Archive a Monday.com item.",
                parameters={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "The item ID."},
                    },
                    "required": ["item_id"],
                },
            ),
            _archive_item,
        )

        # --- status / columns ---

        async def _set_status(
            item_id: int, board_id: int, column_id: str, label: str,
        ) -> dict[str, Any]:
            return await self._get_client().set_status(item_id, board_id, column_id, label)

        _reg(
            ToolDef(
                name="monday_set_status",
                description="Set a status column value on a Monday.com item.",
                parameters={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "The item ID."},
                        "board_id": {"type": "integer", "description": "The board ID."},
                        "column_id": {"type": "string", "description": "The status column ID."},
                        "label": {"type": "string", "description": "The status label to set."},
                    },
                    "required": ["item_id", "board_id", "column_id", "label"],
                },
            ),
            _set_status,
        )

        async def _get_column_values(item_id: int) -> dict[str, Any]:
            return {"column_values": await self._get_client().get_column_values(item_id)}

        _reg(
            ToolDef(
                name="monday_get_column_values",
                description="Get all column values for a Monday.com item.",
                parameters={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "The item ID."},
                    },
                    "required": ["item_id"],
                },
            ),
            _get_column_values,
        )

        # --- updates (comments) ---

        async def _create_update(item_id: int, body: str) -> dict[str, Any]:
            return await self._get_client().create_update(item_id, body)

        _reg(
            ToolDef(
                name="monday_create_update",
                description="Post a comment (update) on a Monday.com item.",
                parameters={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "The item ID."},
                        "body": {"type": "string", "description": "Comment body text."},
                    },
                    "required": ["item_id", "body"],
                },
            ),
            _create_update,
        )

        async def _list_updates(item_id: int, limit: int = 25) -> dict[str, Any]:
            return {"updates": await self._get_client().list_updates(item_id, limit=limit)}

        _reg(
            ToolDef(
                name="monday_list_updates",
                description="List comments (updates) on a Monday.com item.",
                parameters={
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "integer", "description": "The item ID."},
                        "limit": {"type": "integer", "description": "Max updates to return.", "default": 25},
                    },
                    "required": ["item_id"],
                },
            ),
            _list_updates,
        )

        # --- subitems ---

        async def _create_subitem(
            parent_item_id: int,
            item_name: str,
            column_values: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return await self._get_client().create_subitem(
                parent_item_id, item_name, column_values=column_values,
            )

        _reg(
            ToolDef(
                name="monday_create_subitem",
                description="Create a subitem under a Monday.com item.",
                parameters={
                    "type": "object",
                    "properties": {
                        "parent_item_id": {"type": "integer", "description": "Parent item ID."},
                        "item_name": {"type": "string", "description": "Name for the subitem."},
                        "column_values": {"type": "object", "description": "Column values as JSON object."},
                    },
                    "required": ["parent_item_id", "item_name"],
                },
            ),
            _create_subitem,
        )

        async def _list_subitems(parent_item_id: int) -> dict[str, Any]:
            return {"subitems": await self._get_client().list_subitems(parent_item_id)}

        _reg(
            ToolDef(
                name="monday_list_subitems",
                description="List subitems of a Monday.com item.",
                parameters={
                    "type": "object",
                    "properties": {
                        "parent_item_id": {"type": "integer", "description": "Parent item ID."},
                    },
                    "required": ["parent_item_id"],
                },
            ),
            _list_subitems,
        )

    # ── skills ────────────────────────────────────────────────

    def _register_skills(self, ctx: PluginContext) -> None:
        if ctx.skill_registry is None:
            return

        plugin = self.manifest.name

        ctx.skill_registry.register(
            plugin,
            SkillDef(
                name="monday-boards",
                description=(
                    "Monday.com board management — list, inspect boards, "
                    "and manage groups"
                ),
                body=(
                    "You now have access to Monday.com board tools. "
                    "Use monday_list_boards to discover boards, "
                    "monday_get_board for details (columns, groups), "
                    "monday_list_groups to see groups, and "
                    "monday_create_group to add a new group."
                ),
                tools=[
                    "monday_list_boards",
                    "monday_get_board",
                    "monday_list_groups",
                    "monday_create_group",
                ],
            ),
        )

        ctx.skill_registry.register(
            plugin,
            SkillDef(
                name="monday-items",
                description=(
                    "Monday.com item management — list, create, update, "
                    "delete, move, and archive items"
                ),
                body=(
                    "You now have access to Monday.com item tools. "
                    "Use monday_list_items to browse items on a board, "
                    "monday_get_item for full details, monday_create_item "
                    "to add new items, monday_update_item to change column "
                    "values, monday_delete_item to remove, monday_move_item "
                    "to reassign to a group, and monday_archive_item to archive."
                ),
                tools=[
                    "monday_list_items",
                    "monday_get_item",
                    "monday_create_item",
                    "monday_update_item",
                    "monday_delete_item",
                    "monday_move_item",
                    "monday_archive_item",
                ],
            ),
        )

        ctx.skill_registry.register(
            plugin,
            SkillDef(
                name="monday-columns",
                description=(
                    "Monday.com status and column value management"
                ),
                body=(
                    "You now have access to Monday.com column tools. "
                    "Use monday_set_status to update a status column, "
                    "and monday_get_column_values to read all column "
                    "values for an item."
                ),
                tools=[
                    "monday_set_status",
                    "monday_get_column_values",
                ],
            ),
        )

        ctx.skill_registry.register(
            plugin,
            SkillDef(
                name="monday-updates",
                description=(
                    "Monday.com comments (updates) and subitems"
                ),
                body=(
                    "You now have access to Monday.com update and subitem tools. "
                    "Use monday_create_update to post a comment on an item, "
                    "monday_list_updates to read comments, "
                    "monday_create_subitem to create a subitem, and "
                    "monday_list_subitems to list subitems."
                ),
                tools=[
                    "monday_create_update",
                    "monday_list_updates",
                    "monday_create_subitem",
                    "monday_list_subitems",
                ],
            ),
        )


__all__ = ["MondayPlugin", "VAULT_TOKEN_KEY", "VAULT_ACCOUNT_KEY"]
