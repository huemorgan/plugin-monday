"""Async HTTP client for the Monday.com GraphQL API (v2)."""

from __future__ import annotations

import json
from typing import Any

import httpx

API_URL = "https://api.monday.com/v2"
AUTH_URL = "https://auth.monday.com/oauth2/token"


class MondayAPIError(Exception):
    """Raised when Monday.com returns a GraphQL or HTTP error."""


class MondayClient:
    def __init__(self, token: str) -> None:
        self._http = httpx.AsyncClient(
            headers={"Authorization": token, "Content-Type": "application/json"},
            timeout=30.0,
        )

    async def _gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables
        resp = await self._http.post(API_URL, json=body)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data and data["errors"]:
            raise MondayAPIError(data["errors"][0].get("message", str(data["errors"])))
        return data.get("data", {})

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
                "{items{id name state column_values{id title value text}}}}"
            )
            data = await self._gql(q, {"board": board_id, "limit": limit, "col": column_id, "val": value})
            return data.get("items_page_by_column_values", {}).get("items", [])
        q = (
            "query($ids:[ID!]!, $limit:Int!)"
            "{boards(ids:$ids){items_page(limit:$limit){items{id name state column_values{id title value text}}}}}"
        )
        data = await self._gql(q, {"ids": [board_id], "limit": limit})
        boards = data.get("boards", [])
        if not boards:
            return []
        return boards[0].get("items_page", {}).get("items", [])

    async def get_item(self, item_id: int) -> dict[str, Any]:
        q = "query($ids:[ID!]!){items(ids:$ids){id name state board{id name} group{id title} column_values{id title value text} subitems{id name}}}"
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
        q = "query($ids:[ID!]!){items(ids:$ids){column_values{id title value text type}}}"
        data = await self._gql(q, {"ids": [item_id]})
        items = data.get("items", [])
        if not items:
            return []
        return items[0].get("column_values", [])

    # ── updates (comments) ─────────────────────────────────────

    async def create_update(self, item_id: int, body: str) -> dict[str, Any]:
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
        q = "query($ids:[ID!]!){items(ids:$ids){subitems{id name state column_values{id title value text}}}}"
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

    # ── account info ───────────────────────────────────────────

    async def get_account(self) -> dict[str, Any]:
        q = "{me{account{id name slug} name email}}"
        return await self._gql(q)

    # ── lifecycle ──────────────────────────────────────────────

    async def close(self) -> None:
        await self._http.aclose()


async def exchange_code(client_id: str, client_secret: str, code: str) -> dict[str, Any]:
    """Exchange an OAuth authorization code for a token."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(AUTH_URL, json={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        })
        resp.raise_for_status()
        return resp.json()
