# plugin-monday

Monday.com for [Luna](https://github.com/huemorgan/luna) via the Monday
GraphQL API: boards, groups, items, subitems, columns, updates (comments),
webhook change triggers, and raw GraphQL for everything else.

This is a **Luna plugin** built against the Luna Plugin SDK (`luna_sdk`) v0. It
imports nothing from `luna.*` — only the stable SDK surface (including
`SkillDef` and `get_current_user` for route auth) — so it installs from the Luna
marketplace and runs without being part of Luna core.

## Install

In Luna: **Marketplace → Luna Official → plugin-monday → Install**. Then open
**Settings → Monday.com** and click **Connect Monday.com** — approve the popup,
done.

## Auth — no-app OAuth (Dynamic Client Registration)

No monday app to create, no client-id/secret env vars, nothing to install on
the monday side. The plugin self-registers a public OAuth client with monday
(RFC 7591 Dynamic Client Registration on `mcp.monday.com`) and runs the
standard authorization-code + PKCE + state flow in a popup. Access tokens live
7 days and refresh automatically with the issued refresh token.

DCR tokens are rejected by `api.monday.com/v2` (verified: 401), so GraphQL
executes through monday's `all_api_read` / `all_api_write` passthrough on
`mcp.monday.com/mcp` — plain JSON-RPC over HTTPS, no MCP SDK, no session
state, full read/write API surface.

Fallback: paste a **personal API token** (monday.com → avatar → Developers →
My access tokens); that path talks to `api.monday.com/v2` directly and also
serves gateway key-provisioning via `LUNA_MONDAY_API_KEY` /
`LUNA_MONDAY_BASE_URL`.

## What it does

29 skill-gated tools across seven skills:

| Skill | Tools |
|---|---|
| `monday-boards` | list/get/create/archive boards, groups, workspaces, users |
| `monday-items` | list/get/create/update/delete/move/archive items |
| `monday-columns` | set status, get column values, create/delete columns |
| `monday-updates` | create/list updates, create/list subitems |
| `monday-webhooks` | create/list/delete board webhooks (change triggers) |
| `monday-api` | raw GraphQL query + mutation — the whole API |
| `monday-agent` | grant the Luna agent inside monday access to a board/doc |

## Change triggers (webhooks)

`monday_create_webhook` subscribes a board to a monday event
(`create_item`, `change_column_value`, `change_status_column_value`,
`item_deleted`, `create_update`, `when_date_arrived`, ...) delivered to the
plugin's receiver (`/api/p/plugin-monday/webhook/{secret}` — per-install
secret, challenge echo handled). Events re-emit on Luna's event bus as
`monday.*` (`monday.item.created`, `monday.column.changed`,
`monday.status.changed`, ...) for playbooks and schedulers.

## Luna inside monday.com (external agent)

Settings → **Add to Monday.com** lists Luna as an agent in the connected
account (monday's pre-release external-agent API, `API-Version: dev`;
needs the personal-API-token connection — the OAuth/MCP passthrough can't
select the dev version). monday users can then chat with it, @mention it in
an update, or assign it an item.

- Callback URL is minted through **plugin-webhooks** on the luna.com.ai
  gateway, so it survives machine restarts and wakes a sleeping machine.
- Chat replies stream back as SSE; mentions/assignments are acked, then the
  reply is posted in the thread as the agent (falls back to the connected
  user when the agent lacks board access).
- Agents start with no board access: ask Luna to
  "give your monday agent access to board X"
  (`monday_agent_grant_board_access`).
- Requests are verified with `x-monday-signature` (HMAC-SHA256 over
  `{timestamp}.{body}`), a 10-minute freshness window, and a per-install
  path secret.

## Settings UI

Served as a themed **iframe** from the plugin's own managed directory
(`interface/webui/settings/index.html`) — OAuth-first connect button, token
paste behind an expandable detail.

## Layout

```
plugin_monday/
  __init__.py        # the plugin (luna_sdk only) — tools + skills + settings tab
  client.py          # MondayClient (oauth/direct transports) + DCR helpers
  routes.py          # OAuth connect/callback (PKCE+state), status, disconnect, webhook receiver, agent connect/callback
  agent.py           # external-agent helpers: signature, prompt, SSE, reply delivery
  state.py           # process-level MondayClient holder (OAuth hot-swap)
  interface/webui/settings/index.html   # the iframe settings page (OAuth popup)
  luna-plugin.toml   # the data manifest the marketplace reads
```

## License

MIT — see [LICENSE](./LICENSE).
