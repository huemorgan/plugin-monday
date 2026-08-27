"""plugin-monday — Monday.com board/item management via GraphQL.

Connects Luna to Monday.com with the no-app OAuth flow (Dynamic Client
Registration — the user just approves a popup; no monday app is created or
installed) or a pasted personal API token. All tools are skill-gated; the
agent loads monday-boards, monday-items, monday-columns, monday-updates,
monday-webhooks, or monday-api skills to gain access.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from typing import Any

from luna_sdk import (
    CredentialSlot,
    LunaPlugin,
    PluginContext,
    PluginManifest,
    SettingsTab,
    SkillDef,
    ToolDef,
)

from .client import MondayClient, client_from_bundle
from .state import get_client, set_client

log = logging.getLogger("plugin-monday")

VAULT_TOKEN_KEY = "plugin_monday.oauth"
VAULT_ACCOUNT_KEY = "plugin_monday.account_id"
VAULT_OAUTH_BUNDLE_KEY = "plugin_monday.oauth2"
VAULT_WEBHOOK_SECRET_KEY = "plugin_monday.webhook_secret"
ENV_KEY = "LUNA_MONDAY_API_KEY"
ENV_BASE_URL = "LUNA_MONDAY_BASE_URL"


def find_webhooks_plugin():
    """The live plugin-webhooks instance, or None when not installed.

    The loader imports in-tree plugins as ``plugin_webhooks`` but managed
    (marketplace-installed) ones under a synthetic name
    (``luna_plugin_plugin_webhooks``), so check both — and fall back to a
    sys.modules scan in case the naming scheme shifts again.
    """
    import sys as _sys

    candidates = ["plugin_webhooks", "luna_plugin_plugin_webhooks"]
    candidates += [
        n for n in list(_sys.modules)
        if n.endswith("plugin_webhooks") and n not in candidates
    ]
    for name in candidates:
        mod = _sys.modules.get(name)
        state = getattr(mod, "state", None)
        get = getattr(state, "get_plugin", None)
        if callable(get):
            live = get()
            if live is not None:
                return live
    return None


class MondayPlugin(LunaPlugin):
    manifest = PluginManifest(
        name="plugin-monday",
        shown_name="Monday.com",
        icon="kanban",
        image="assets/icon.png",
        version="0.4.1",
        description="Monday.com boards, items, webhooks, and full API access via GraphQL.",
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

    def credential_slots(self) -> list[CredentialSlot]:
        # env_base_url_var marks monday proxy-provisionable: the gateway sets
        # LUNA_MONDAY_BASE_URL (={gateway}/proxy/monday) + the token via
        # LUNA_MONDAY_API_KEY. Only direct GraphQL calls proxy; OAuth stays direct.
        return [
            CredentialSlot(
                slug="monday",
                credential_name=VAULT_TOKEN_KEY,
                env_key_var=ENV_KEY,
                env_base_url_var=ENV_BASE_URL,
                owner=self.manifest.name,
            )
        ]

    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        set_client(None)

        # Auth precedence: OAuth bundle (the no-app popup flow) → vault token
        # (pasted personal token) → env (gateway token in proxy mode).
        bundle = await self._resolve_bundle(ctx)
        if bundle:
            set_client(client_from_bundle(bundle, on_refresh=self._persist_bundle))
        else:
            token = await self._resolve_token(ctx)
            if token:
                set_client(MondayClient(token, base_url=self._resolve_base_url(ctx)))

        self._register_tools(ctx)
        self._register_skills(ctx)
        log.info(
            "plugin-monday loaded (tools=28, connected=%s, transport=%s)",
            get_client() is not None,
            getattr(get_client(), "transport", None),
        )

    async def _resolve_bundle(self, ctx: PluginContext) -> dict[str, Any] | None:
        vault = getattr(ctx, "vault", None)
        if vault is None:
            return None
        try:
            raw = (await vault.get_credential(VAULT_OAUTH_BUNDLE_KEY)).value
            bundle = json.loads(raw)
            if bundle.get("access_token") and bundle.get("client_id"):
                return bundle
        except KeyError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("plugin-monday: oauth bundle read failed: %s", exc)
        return None

    async def _persist_bundle(self, bundle: dict[str, Any]) -> None:
        vault = getattr(self._ctx, "vault", None)
        if vault is not None:
            await vault.store_credential(
                VAULT_OAUTH_BUNDLE_KEY, json.dumps(bundle), kind="oauth",
            )

    async def _resolve_token(self, ctx: PluginContext) -> str | None:
        vault = getattr(ctx, "vault", None)
        if vault is not None:
            try:
                cred = await vault.get_credential(VAULT_TOKEN_KEY)
                if (cred.value or "").strip():
                    return cred.value.strip()
            except KeyError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning("plugin-monday: vault read failed: %s", exc)
        if getattr(ctx, "get_env", None) is not None:
            val = (ctx.get_env(ENV_KEY) or "").strip()
            if val:
                return val
        return (os.environ.get("MONDAY_API_KEY") or "").strip() or None

    def _resolve_base_url(self, ctx: PluginContext) -> str | None:
        if getattr(ctx, "get_env", None) is not None:
            val = (ctx.get_env(ENV_BASE_URL) or "").strip()
            if val:
                return val
        return (os.environ.get("MONDAY_BASE_URL") or "").strip() or None

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

    @staticmethod
    def _webhooks_plugin():
        return find_webhooks_plugin()

    async def _webhook_url(self) -> str:
        """Public URL monday should deliver webhook events to.

        Minted through plugin-webhooks so deliveries wake a sleeping
        machine instead of being lost; there is no direct-URL fallback.
        Sync mode is required — the gateway's queue path can't echo
        Monday's registration challenge.
        """
        vault = getattr(self._ctx, "vault", None)
        if vault is None:
            raise RuntimeError("Vault not available")
        try:
            secret = (await vault.get_credential(VAULT_WEBHOOK_SECRET_KEY)).value
        except KeyError:
            secret = secrets.token_urlsafe(24)
            await vault.store_credential(VAULT_WEBHOOK_SECRET_KEY, secret, kind="metadata")

        webhooks = self._webhooks_plugin()
        if webhooks is None:
            raise RuntimeError(
                "Monday.com triggers need the Webhooks plugin. Install "
                "'plugin-webhooks' from the Marketplace, then try again — "
                "it gives Monday a stable public URL that wakes this agent."
            )
        hook = await webhooks.create_hook(
            "monday-events",
            target=f"/api/p/plugin-monday/webhook/{secret}",
            mode="sync",
            plugin="plugin-monday",
        )
        url = hook.get("public_url")
        if not url:
            raise RuntimeError("Webhooks plugin returned no public URL for the Monday hook.")
        return url

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

        async def _create_board(
            board_name: str,
            board_kind: str = "public",
            workspace_id: int | None = None,
            description: str | None = None,
        ) -> dict[str, Any]:
            return await self._get_client().create_board(
                board_name, board_kind=board_kind,
                workspace_id=workspace_id, description=description,
            )

        _reg(
            ToolDef(
                name="monday_create_board",
                description="Create a new Monday.com board.",
                parameters={
                    "type": "object",
                    "properties": {
                        "board_name": {"type": "string", "description": "Name for the new board."},
                        "board_kind": {"type": "string", "description": "public, private, or share.", "default": "public"},
                        "workspace_id": {"type": "integer", "description": "Workspace to create the board in."},
                        "description": {"type": "string", "description": "Board description."},
                    },
                    "required": ["board_name"],
                },
            ),
            _create_board,
        )

        async def _archive_board(board_id: int) -> dict[str, Any]:
            return await self._get_client().archive_board(board_id)

        _reg(
            ToolDef(
                name="monday_archive_board",
                description="Archive a Monday.com board.",
                parameters={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "integer", "description": "The board ID."},
                    },
                    "required": ["board_id"],
                },
                risk_level="high",
            ),
            _archive_board,
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

        # --- workspaces / users ---

        async def _list_workspaces(limit: int = 50) -> dict[str, Any]:
            return {"workspaces": await self._get_client().list_workspaces(limit=limit)}

        _reg(
            ToolDef(
                name="monday_list_workspaces",
                description="List Monday.com workspaces.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max workspaces to return.", "default": 50},
                    },
                },
            ),
            _list_workspaces,
        )

        async def _list_users(limit: int = 100) -> dict[str, Any]:
            return {"users": await self._get_client().list_users(limit=limit)}

        _reg(
            ToolDef(
                name="monday_list_users",
                description="List users in the Monday.com account (for assignments).",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max users to return.", "default": 100},
                    },
                },
            ),
            _list_users,
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

        async def _create_column(
            board_id: int, title: str, column_type: str,
            defaults: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return await self._get_client().create_column(
                board_id, title, column_type, defaults=defaults,
            )

        _reg(
            ToolDef(
                name="monday_create_column",
                description=(
                    "Add a column to a Monday.com board. column_type is a monday "
                    "ColumnType like status, text, numbers, date, people, checkbox."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "integer", "description": "The board ID."},
                        "title": {"type": "string", "description": "Column title."},
                        "column_type": {"type": "string", "description": "monday ColumnType (status, text, numbers, date, people, ...)."},
                        "defaults": {"type": "object", "description": "Type-specific default settings (e.g. status labels)."},
                    },
                    "required": ["board_id", "title", "column_type"],
                },
            ),
            _create_column,
        )

        async def _delete_column(board_id: int, column_id: str) -> dict[str, Any]:
            return await self._get_client().delete_column(board_id, column_id)

        _reg(
            ToolDef(
                name="monday_delete_column",
                description="Delete a column (and all its values) from a Monday.com board.",
                parameters={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "integer", "description": "The board ID."},
                        "column_id": {"type": "string", "description": "The column ID."},
                    },
                    "required": ["board_id", "column_id"],
                },
                risk_level="high",
            ),
            _delete_column,
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

        # --- webhooks (change triggers) ---

        async def _create_webhook(
            board_id: int, event: str, config: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            url = await self._webhook_url()
            hook = await self._get_client().create_webhook(
                board_id, url, event, config=config,
            )
            return {"webhook": hook, "delivers_to": url}

        _reg(
            ToolDef(
                name="monday_create_webhook",
                description=(
                    "Subscribe to change events on a Monday.com board. Requires "
                    "the Webhooks plugin (plugin-webhooks) — if it is not "
                    "installed, tell the user to install it from the Marketplace "
                    "first. Events are "
                    "delivered to Luna and re-emitted on the event bus as monday.* "
                    "(e.g. monday.item.created, monday.column.changed). Common "
                    "event values: create_item, change_column_value, "
                    "change_status_column_value, change_specific_column_value, "
                    "item_deleted, item_archived, item_moved_to_any_group, "
                    "create_update, create_subitem, when_date_arrived. "
                    "change_specific_column_value takes config {\"columnId\": ...}."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "integer", "description": "The board ID to watch."},
                        "event": {"type": "string", "description": "monday WebhookEventType (e.g. create_item, change_column_value)."},
                        "config": {"type": "object", "description": "Event config, e.g. {\"columnId\": \"status\"} for column-specific events."},
                    },
                    "required": ["board_id", "event"],
                },
            ),
            _create_webhook,
        )

        async def _list_webhooks(board_id: int) -> dict[str, Any]:
            return {"webhooks": await self._get_client().list_webhooks(board_id)}

        _reg(
            ToolDef(
                name="monday_list_webhooks",
                description="List webhooks registered on a Monday.com board.",
                parameters={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "integer", "description": "The board ID."},
                    },
                    "required": ["board_id"],
                },
            ),
            _list_webhooks,
        )

        async def _delete_webhook(webhook_id: int) -> dict[str, Any]:
            return await self._get_client().delete_webhook(webhook_id)

        _reg(
            ToolDef(
                name="monday_delete_webhook",
                description="Delete a Monday.com webhook subscription.",
                parameters={
                    "type": "object",
                    "properties": {
                        "webhook_id": {"type": "integer", "description": "The webhook ID."},
                    },
                    "required": ["webhook_id"],
                },
            ),
            _delete_webhook,
        )

        # --- raw API (anything the dedicated tools don't cover) ---

        async def _api_query(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
            if query.lstrip().startswith("mutation"):
                raise RuntimeError("Use monday_api_mutate for mutations.")
            return await self._get_client().api(query, variables)

        _reg(
            ToolDef(
                name="monday_api_query",
                description=(
                    "Run any read-only GraphQL query against the Monday.com API. "
                    "Covers every API read the dedicated tools don't."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "GraphQL query document."},
                        "variables": {"type": "object", "description": "GraphQL variables."},
                    },
                    "required": ["query"],
                },
            ),
            _api_query,
        )

        async def _api_mutate(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
            if not query.lstrip().startswith("mutation"):
                raise RuntimeError("monday_api_mutate only accepts mutation documents.")
            return await self._get_client().api(query, variables)

        _reg(
            ToolDef(
                name="monday_api_mutate",
                description=(
                    "Run any GraphQL mutation against the Monday.com API. "
                    "Covers every API write the dedicated tools don't."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "GraphQL mutation document."},
                        "variables": {"type": "object", "description": "GraphQL variables."},
                    },
                    "required": ["query"],
                },
                risk_level="high",
            ),
            _api_mutate,
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
                    "Monday.com board management — list, inspect, create, archive "
                    "boards; groups, workspaces, and users"
                ),
                body=(
                    "You now have access to Monday.com board tools. "
                    "Use monday_list_boards to discover boards, "
                    "monday_get_board for details (columns, groups), "
                    "monday_create_board / monday_archive_board to manage boards, "
                    "monday_list_groups / monday_create_group for groups, "
                    "monday_list_workspaces for workspaces, and "
                    "monday_list_users to resolve people for assignments."
                ),
                tools=[
                    "monday_list_boards",
                    "monday_get_board",
                    "monday_create_board",
                    "monday_archive_board",
                    "monday_list_groups",
                    "monday_create_group",
                    "monday_list_workspaces",
                    "monday_list_users",
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
                    "Monday.com column management — statuses, column values, "
                    "add or remove board columns"
                ),
                body=(
                    "You now have access to Monday.com column tools. "
                    "Use monday_set_status to update a status column, "
                    "monday_get_column_values to read an item's values, "
                    "monday_create_column to add a column to a board, and "
                    "monday_delete_column to remove one."
                ),
                tools=[
                    "monday_set_status",
                    "monday_get_column_values",
                    "monday_create_column",
                    "monday_delete_column",
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

        ctx.skill_registry.register(
            plugin,
            SkillDef(
                name="monday-webhooks",
                description=(
                    "Monday.com change triggers — subscribe boards to webhook "
                    "events that fire Luna events on item/column/status changes"
                ),
                body=(
                    "You now have access to Monday.com webhook tools. "
                    "They deliver through the Webhooks plugin (plugin-webhooks), "
                    "which must be installed — if a webhook tool reports it is "
                    "missing, ask the user to install plugin-webhooks from the "
                    "Marketplace, then retry. "
                    "Use monday_create_webhook to watch a board for changes "
                    "(item created, column changed, status changed, item deleted, "
                    "comment posted, date arrived, ...). Incoming events are "
                    "re-emitted on Luna's event bus as monday.* events "
                    "(monday.item.created, monday.column.changed, "
                    "monday.status.changed, monday.item.deleted, ...) which "
                    "playbooks and schedulers can react to. "
                    "monday_list_webhooks shows what a board is subscribed to; "
                    "monday_delete_webhook unsubscribes."
                ),
                tools=[
                    "monday_create_webhook",
                    "monday_list_webhooks",
                    "monday_delete_webhook",
                ],
            ),
        )

        ctx.skill_registry.register(
            plugin,
            SkillDef(
                name="monday-api",
                description=(
                    "Raw Monday.com GraphQL — any API operation the dedicated "
                    "tools don't cover (docs, dashboards, tags, teams, assets, ...)"
                ),
                body=(
                    "You now have raw access to the Monday.com GraphQL API. "
                    "Use monday_api_query for reads and monday_api_mutate for "
                    "writes. Pass GraphQL variables as a JSON object; use "
                    "monday_get_board first when you need column IDs."
                ),
                tools=[
                    "monday_api_query",
                    "monday_api_mutate",
                ],
            ),
        )


__all__ = ["MondayPlugin", "VAULT_TOKEN_KEY", "VAULT_ACCOUNT_KEY", "VAULT_OAUTH_BUNDLE_KEY"]
