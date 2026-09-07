"""Async client for the Monday.com GraphQL API.

Two transports, one GraphQL surface:

- **oauth** — the no-app OAuth path. Tokens come from monday's Dynamic Client
  Registration flow (RFC 7591) on ``mcp.monday.com``; no monday app has to be
  created or installed. Those tokens are rejected by ``api.monday.com/v2``
  (verified empirically: 401), so GraphQL is executed through the
  ``all_api_read`` / ``all_api_write`` passthrough tools on monday's MCP server
  via plain JSON-RPC over HTTP — no MCP SDK, no session state. Access tokens
  live 7 days and are auto-refreshed with the refresh token.

- **direct** — a personal API token (or a gateway-provisioned token via
  ``base_url``) posted straight to ``api.monday.com/v2``.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable

import httpx

API_URL = "https://api.monday.com/v2"

MCP_BASE = "https://mcp.monday.com"
MCP_RPC_URL = f"{MCP_BASE}/mcp"
MCP_REGISTER_URL = f"{MCP_BASE}/register"
MCP_AUTHORIZE_URL = f"{MCP_BASE}/authorize"
MCP_TOKEN_URL = f"{MCP_BASE}/token"

_MUTATION_RE = re.compile(r"^\s*mutation\b")

# External agents are a pre-release feature: every call needs API-Version: dev,
# and connect_external_agent_sync blocks ~25 s (monday asks for ≥40 s timeouts).
AGENT_API_VERSION = "dev"
AGENT_CONNECT_TIMEOUT_S = 60.0

# Board events monday accepts in the create_webhook mutation.
WEBHOOK_EVENTS = [
    "create_item", "change_name", "change_column_value",
    "change_status_column_value", "change_specific_column_value",
    "item_moved_to_any_group", "item_moved_to_specific_group",
    "item_archived", "item_deleted", "item_restored",
    "create_subitem", "change_subitem_name", "change_subitem_column_value",
    "move_subitem", "subitem_archived", "subitem_deleted",
    "create_update", "edit_update", "delete_update", "create_subitem_update",
    "when_date_arrived",
]


class MondayAPIError(Exception):
    """Raised when Monday.com returns a GraphQL or HTTP error."""


class MondayClient:
    def __init__(
        self,
        token: str,
        base_url: str | None = None,
        *,
        oauth: dict[str, Any] | None = None,
        on_refresh: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        # `oauth` switches to the MCP passthrough transport. It carries
        # client_id + refresh_token + expires_at (epoch seconds). `on_refresh`
        # is awaited with the updated bundle after every token refresh so the
        # caller can persist it. Without `oauth`, `token` is a personal API
        # token (or gateway token when `base_url` points at the proxy) and
        # GraphQL goes straight to api.monday.com/v2.
        self._token = token
        self._oauth = dict(oauth) if oauth else None
        self._on_refresh = on_refresh
        self._api_url = (base_url or API_URL).rstrip("/")
        self._http = httpx.AsyncClient(timeout=30.0)

    @property
    def transport(self) -> str:
        return "oauth" if self._oauth else "direct"

    async def _gql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        api_version: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self._oauth is not None:
            # The MCP passthrough pins its own API version; dev-only fields
            # (external agents) surface as "Cannot query field" errors there.
            return await self._gql_mcp(query, variables)
        return await self._gql_direct(query, variables, api_version=api_version, timeout=timeout)

    async def _gql_direct(
        self,
        query: str,
        variables: dict[str, Any] | None,
        *,
        api_version: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables
        headers = {"Authorization": self._token, "Content-Type": "application/json"}
        if api_version:
            headers["API-Version"] = api_version
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await self._http.post(self._api_url, json=body, headers=headers, **kwargs)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data and data["errors"]:
            raise MondayAPIError(data["errors"][0].get("message", str(data["errors"])))
        return data.get("data", {})

    async def _gql_mcp(self, query: str, variables: dict[str, Any] | None) -> dict[str, Any]:
        if self._token_stale():
            await self._refresh()
        resp = await self._post_mcp(query, variables)
        if resp.status_code == 401:
            await self._refresh()
            resp = await self._post_mcp(query, variables)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise MondayAPIError(data["error"].get("message", str(data["error"])))
        result = data.get("result", {})
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
            raise MondayAPIError(" ".join(texts) or "monday MCP tool call failed")
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            return sc
        for c in result.get("content", []):
            if c.get("type") == "text":
                return json.loads(c["text"])
        return {}

    async def _post_mcp(self, query: str, variables: dict[str, Any] | None) -> httpx.Response:
        tool = "all_api_write" if _MUTATION_RE.match(query) else "all_api_read"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                # The passthrough tools require `variables` as a JSON *string*.
                "arguments": {"query": query, "variables": json.dumps(variables or {})},
            },
        }
        return await self._http.post(
            MCP_RPC_URL, json=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

    def _token_stale(self) -> bool:
        exp = (self._oauth or {}).get("expires_at")
        return bool(exp) and time.time() > float(exp) - 120

    async def _refresh(self) -> None:
        assert self._oauth is not None
        tok = await dcr_refresh(self._oauth["client_id"], self._oauth["refresh_token"])
        self._token = tok["access_token"]
        self._oauth["refresh_token"] = tok.get("refresh_token", self._oauth["refresh_token"])
        self._oauth["expires_at"] = time.time() + float(tok.get("expires_in", 3600))
        if self._on_refresh is not None:
            await self._on_refresh({
                "access_token": self._token,
                "refresh_token": self._oauth["refresh_token"],
                "expires_at": self._oauth["expires_at"],
                "client_id": self._oauth["client_id"],
            })

    # ── raw API ────────────────────────────────────────────────

    async def api(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute arbitrary GraphQL against the Monday.com API."""
        return await self._gql(query, variables)

    # ── boards ─────────────────────────────────────────────────

    async def list_boards(
        self, *, limit: int = 25, workspace_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if workspace_id:
            q = "query($limit:Int!, $ws:ID!){boards(limit:$limit, workspace_ids:[$ws]){id name description board_kind state}}"
            data = await self._gql(q, {"limit": limit, "ws": workspace_id})
        else:
            q = "query($limit:Int!){boards(limit:$limit){id name description board_kind state}}"
            data = await self._gql(q, {"limit": limit})
        return data.get("boards", [])

    async def get_board(self, board_id: int) -> dict[str, Any]:
        q = "query($ids:[ID!]!){boards(ids:$ids){id name description board_kind state columns{id title type settings_str} groups{id title}}}"
        data = await self._gql(q, {"ids": [board_id]})
        boards = data.get("boards", [])
        if not boards:
            raise MondayAPIError(f"Board {board_id} not found")
        return boards[0]

    async def create_board(
        self,
        board_name: str,
        *,
        board_kind: str = "public",
        workspace_id: int | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        parts = ["$name:String!", "$kind:BoardKind!"]
        args = "board_name:$name, board_kind:$kind"
        variables: dict[str, Any] = {"name": board_name, "kind": board_kind}
        if workspace_id:
            parts.append("$ws:ID")
            args += ", workspace_id:$ws"
            variables["ws"] = workspace_id
        if description:
            parts.append("$desc:String")
            args += ", description:$desc"
            variables["desc"] = description
        q = f"mutation({', '.join(parts)}){{create_board({args}){{id name}}}}"
        data = await self._gql(q, variables)
        return data.get("create_board", {})

    async def archive_board(self, board_id: int) -> dict[str, Any]:
        q = "mutation($id:ID!){archive_board(board_id:$id){id state}}"
        data = await self._gql(q, {"id": board_id})
        return data.get("archive_board", {})

    # ── columns ────────────────────────────────────────────────

    async def create_column(
        self,
        board_id: int,
        title: str,
        column_type: str,
        *,
        defaults: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parts = ["$board:ID!", "$title:String!", "$type:ColumnType!"]
        args = "board_id:$board, title:$title, column_type:$type"
        variables: dict[str, Any] = {"board": board_id, "title": title, "type": column_type}
        if defaults:
            parts.append("$defaults:JSON")
            args += ", defaults:$defaults"
            variables["defaults"] = json.dumps(defaults)
        q = f"mutation({', '.join(parts)}){{create_column({args}){{id title type}}}}"
        data = await self._gql(q, variables)
        return data.get("create_column", {})

    async def delete_column(self, board_id: int, column_id: str) -> dict[str, Any]:
        q = "mutation($board:ID!, $col:String!){delete_column(board_id:$board, column_id:$col){id}}"
        data = await self._gql(q, {"board": board_id, "col": column_id})
        return data.get("delete_column", {})

    # ── workspaces / users ─────────────────────────────────────

    async def list_workspaces(self, *, limit: int = 50) -> list[dict[str, Any]]:
        q = "query($limit:Int!){workspaces(limit:$limit){id name kind description}}"
        data = await self._gql(q, {"limit": limit})
        return data.get("workspaces", [])

    async def list_users(self, *, limit: int = 100) -> list[dict[str, Any]]:
        q = "query($limit:Int!){users(limit:$limit){id name email title is_guest}}"
        data = await self._gql(q, {"limit": limit})
        return data.get("users", [])

    # ── items ──────────────────────────────────────────────────

    async def list_items(
        self,
        board_id: int,
        *,
        limit: int = 25,
        column_id: str | None = None,
        value: str | None = None,
    ) -> list[dict[str, Any]]:
        if column_id and value:
            q = (
                "query($board:ID!, $limit:Int!, $col:String!, $val:CompareValue!)"
                "{items_page_by_column_values(board_id:$board, limit:$limit, columns:[{column_id:$col, column_values:[$val]}])"
                "{items{id name state column_values{id text value}}}}"
            )
            data = await self._gql(q, {"board": board_id, "limit": limit, "col": column_id, "val": value})
            return data.get("items_page_by_column_values", {}).get("items", [])
        q = (
            "query($ids:[ID!]!, $limit:Int!)"
            "{boards(ids:$ids){items_page(limit:$limit){items{id name state column_values{id text value}}}}}"
        )
        data = await self._gql(q, {"ids": [board_id], "limit": limit})
        boards = data.get("boards", [])
        if not boards:
            return []
        return boards[0].get("items_page", {}).get("items", [])

    async def get_item(self, item_id: int) -> dict[str, Any]:
        q = "query($ids:[ID!]!){items(ids:$ids){id name state board{id name} group{id title} column_values{id text value} subitems{id name}}}"
        data = await self._gql(q, {"ids": [item_id]})
        items = data.get("items", [])
        if not items:
            raise MondayAPIError(f"Item {item_id} not found")
        return items[0]

    async def create_item(
        self,
        board_id: int,
        item_name: str,
        *,
        group_id: str | None = None,
        column_values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parts = ["$board:ID!", "$name:String!"]
        args = "board_id:$board, item_name:$name"
        variables: dict[str, Any] = {"board": board_id, "name": item_name}
        if group_id:
            parts.append("$group:String!")
            args += ", group_id:$group"
            variables["group"] = group_id
        if column_values:
            parts.append("$cols:JSON!")
            args += ", column_values:$cols"
            variables["cols"] = json.dumps(column_values)
        q = f"mutation({', '.join(parts)}){{create_item({args}){{id name}}}}"
        data = await self._gql(q, variables)
        return data.get("create_item", {})

    async def update_item(
        self, item_id: int, board_id: int, column_values: dict[str, Any],
    ) -> dict[str, Any]:
        q = "mutation($board:ID!, $item:ID!, $cols:JSON!){change_multiple_column_values(board_id:$board, item_id:$item, column_values:$cols){id name}}"
        data = await self._gql(q, {
            "board": board_id,
            "item": item_id,
            "cols": json.dumps(column_values),
        })
        return data.get("change_multiple_column_values", {})

    async def delete_item(self, item_id: int) -> dict[str, Any]:
        q = "mutation($id:ID!){delete_item(item_id:$id){id}}"
        data = await self._gql(q, {"id": item_id})
        return data.get("delete_item", {})

    async def move_item(self, item_id: int, group_id: str) -> dict[str, Any]:
        q = "mutation($item:ID!, $group:String!){move_item_to_group(item_id:$item, group_id:$group){id}}"
        data = await self._gql(q, {"item": item_id, "group": group_id})
        return data.get("move_item_to_group", {})

    async def archive_item(self, item_id: int) -> dict[str, Any]:
        q = "mutation($id:ID!){archive_item(item_id:$id){id}}"
        data = await self._gql(q, {"id": item_id})
        return data.get("archive_item", {})

    # ── status / columns ───────────────────────────────────────

    async def set_status(
        self, item_id: int, board_id: int, column_id: str, label: str,
    ) -> dict[str, Any]:
        q = "mutation($board:ID!, $item:ID!, $col:String!, $val:JSON!){change_column_value(board_id:$board, item_id:$item, column_id:$col, value:$val){id}}"
        data = await self._gql(q, {
            "board": board_id,
            "item": item_id,
            "col": column_id,
            "val": json.dumps({"label": label}),
        })
        return data.get("change_column_value", {})

    async def get_column_values(self, item_id: int) -> list[dict[str, Any]]:
        q = "query($ids:[ID!]!){items(ids:$ids){column_values{id text value type}}}"
        data = await self._gql(q, {"ids": [item_id]})
        items = data.get("items", [])
        if not items:
            return []
        return items[0].get("column_values", [])

    # ── updates (comments) ─────────────────────────────────────

    async def create_update(
        self, item_id: int, body: str, *, parent_id: int | str | None = None,
    ) -> dict[str, Any]:
        if parent_id:
            q = "mutation($item:ID!, $body:String!, $parent:ID){create_update(item_id:$item, body:$body, parent_id:$parent){id body created_at}}"
            data = await self._gql(q, {"item": item_id, "body": body, "parent": str(parent_id)})
        else:
            q = "mutation($item:ID!, $body:String!){create_update(item_id:$item, body:$body){id body created_at}}"
            data = await self._gql(q, {"item": item_id, "body": body})
        return data.get("create_update", {})

    async def list_updates(self, item_id: int, *, limit: int = 25) -> list[dict[str, Any]]:
        q = "query($ids:[ID!]!, $limit:Int!){items(ids:$ids){updates(limit:$limit){id body created_at creator{name}}}}"
        data = await self._gql(q, {"ids": [item_id], "limit": limit})
        items = data.get("items", [])
        if not items:
            return []
        return items[0].get("updates", [])

    # ── subitems ───────────────────────────────────────────────

    async def create_subitem(
        self,
        parent_item_id: int,
        item_name: str,
        *,
        column_values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if column_values:
            q = "mutation($parent:ID!, $name:String!, $cols:JSON!){create_subitem(parent_item_id:$parent, item_name:$name, column_values:$cols){id name}}"
            data = await self._gql(q, {
                "parent": parent_item_id,
                "name": item_name,
                "cols": json.dumps(column_values),
            })
        else:
            q = "mutation($parent:ID!, $name:String!){create_subitem(parent_item_id:$parent, item_name:$name){id name}}"
            data = await self._gql(q, {"parent": parent_item_id, "name": item_name})
        return data.get("create_subitem", {})

    async def list_subitems(self, parent_item_id: int) -> list[dict[str, Any]]:
        q = "query($ids:[ID!]!){items(ids:$ids){subitems{id name state column_values{id text value}}}}"
        data = await self._gql(q, {"ids": [parent_item_id]})
        items = data.get("items", [])
        if not items:
            return []
        return items[0].get("subitems", [])

    # ── groups ─────────────────────────────────────────────────

    async def list_groups(self, board_id: int) -> list[dict[str, Any]]:
        q = "query($ids:[ID!]!){boards(ids:$ids){groups{id title color position}}}"
        data = await self._gql(q, {"ids": [board_id]})
        boards = data.get("boards", [])
        if not boards:
            return []
        return boards[0].get("groups", [])

    async def create_group(self, board_id: int, group_name: str) -> dict[str, Any]:
        q = "mutation($board:ID!, $name:String!){create_group(board_id:$board, group_name:$name){id title}}"
        data = await self._gql(q, {"board": board_id, "name": group_name})
        return data.get("create_group", {})

    # ── webhooks ───────────────────────────────────────────────

    async def create_webhook(
        self,
        board_id: int,
        url: str,
        event: str,
        *,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parts = ["$board:ID!", "$url:String!", "$event:WebhookEventType!"]
        args = "board_id:$board, url:$url, event:$event"
        variables: dict[str, Any] = {"board": board_id, "url": url, "event": event}
        if config:
            parts.append("$config:JSON")
            args += ", config:$config"
            variables["config"] = json.dumps(config)
        q = f"mutation({', '.join(parts)}){{create_webhook({args}){{id board_id event config}}}}"
        data = await self._gql(q, variables)
        return data.get("create_webhook", {})

    async def list_webhooks(self, board_id: int) -> list[dict[str, Any]]:
        q = "query($board:ID!){webhooks(board_id:$board){id event board_id config}}"
        data = await self._gql(q, {"board": board_id})
        return data.get("webhooks", []) or []

    async def delete_webhook(self, webhook_id: int) -> dict[str, Any]:
        q = "mutation($id:ID!){delete_webhook(id:$id){id board_id}}"
        data = await self._gql(q, {"id": webhook_id})
        return data.get("delete_webhook", {})

    # ── external agent (pre-release API, needs API-Version: dev) ──

    async def connect_external_agent(self, name: str, callback_url: str) -> dict[str, Any]:
        """Create Luna as a custom agent in the account. monday takes ~25 s.
        Returns agent_id + signing_secret + api_token — shown once only."""
        q = (
            "mutation($input:ConnectExternalAgentSyncInput!){"
            "connect_external_agent_sync(input:$input){agent_id signing_secret api_token instructions}}"
        )
        data = await self._gql(
            q, {"input": {"custom": {"name": name, "callback_url": callback_url}}},
            api_version=AGENT_API_VERSION, timeout=AGENT_CONNECT_TIMEOUT_S,
        )
        return data.get("connect_external_agent_sync", {}) or {}

    async def activate_agent(self, agent_id: int | str) -> dict[str, Any]:
        q = "mutation($id:ID!){activate_agent(id:$id){success}}"
        data = await self._gql(q, {"id": str(agent_id)}, api_version=AGENT_API_VERSION)
        return data.get("activate_agent", {}) or {}

    async def update_custom_agent(
        self,
        agent_id: int | str,
        *,
        name: str | None = None,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        """Rename or re-point the agent. A callback_url change rotates the
        signing secret (returned); name-only updates return signing_secret null."""
        inp: dict[str, Any] = {"agent_id": str(agent_id)}
        if name:
            inp["name"] = name
        if callback_url:
            inp["callback_url"] = callback_url
        q = "mutation($input:UpdateCustomAgentInput!){update_custom_agent(input:$input){success signing_secret}}"
        data = await self._gql(q, {"input": inp}, api_version=AGENT_API_VERSION)
        return data.get("update_custom_agent", {}) or {}

    async def disconnect_external_agent(self, agent_id: int | str) -> dict[str, Any]:
        q = "mutation($id:ID!){disconnect_external_agent(id:$id){success}}"
        data = await self._gql(q, {"id": str(agent_id)}, api_version=AGENT_API_VERSION)
        return data.get("disconnect_external_agent", {}) or {}

    async def add_agent_resource_access(
        self,
        agent_id: int | str,
        resource_id: int | str,
        *,
        scope_type: str = "BOARD",
        permission_type: str = "READ_WRITE",
    ) -> dict[str, Any]:
        """Agents don't inherit the creator's access — grant boards/docs one by one."""
        q = (
            "mutation($id:ID!, $res:ID!, $scope:AgentResourceScopeType!, $perm:AgentResourcePermissionType!){"
            "add_agent_resource_access(id:$id, resource_id:$res, scope_type:$scope, permission_type:$perm){success}}"
        )
        data = await self._gql(
            q, {"id": str(agent_id), "res": str(resource_id), "scope": scope_type, "perm": permission_type},
            api_version=AGENT_API_VERSION,
        )
        return data.get("add_agent_resource_access", {}) or {}

    # ── account info ───────────────────────────────────────────

    async def get_account(self) -> dict[str, Any]:
        q = "{me{account{id name slug} name email}}"
        return await self._gql(q)

    # ── lifecycle ──────────────────────────────────────────────

    async def close(self) -> None:
        await self._http.aclose()


def client_from_bundle(
    bundle: dict[str, Any],
    on_refresh: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> MondayClient:
    """Build an OAuth-transport client from a persisted token bundle."""
    return MondayClient(
        bundle["access_token"],
        oauth={
            "client_id": bundle["client_id"],
            "refresh_token": bundle["refresh_token"],
            "expires_at": bundle.get("expires_at"),
        },
        on_refresh=on_refresh,
    )


# ── OAuth Dynamic Client Registration (no monday app needed) ───


async def dcr_register(redirect_uri: str, client_name: str = "Luna") -> dict[str, Any]:
    """Self-register a public OAuth client with monday (RFC 7591)."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(MCP_REGISTER_URL, json={
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
        resp.raise_for_status()
        return resp.json()


async def dcr_exchange_code(
    client_id: str, code: str, redirect_uri: str, code_verifier: str,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens (PKCE, no client secret)."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(MCP_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        })
        resp.raise_for_status()
        return resp.json()


async def dcr_refresh(client_id: str, refresh_token: str) -> dict[str, Any]:
    """Trade a refresh token for a fresh access token."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(MCP_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        })
        resp.raise_for_status()
        return resp.json()
