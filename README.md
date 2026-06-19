# plugin-monday

Monday.com board and item management for [Luna](https://github.com/huemorgan/luna)
via the Monday GraphQL API: boards, groups, items, subitems, status/columns, and
updates (comments).

This is a **Luna plugin** built against the Luna Plugin SDK (`luna_sdk`) v0. It
imports nothing from `luna.*` — only the stable SDK surface (including
`SkillDef` and `get_current_user` for route auth) — so it installs from the Luna
marketplace and runs without being part of Luna core.

## Install

In Luna: **Marketplace → Luna Official → plugin-monday → Install**. Then open
**Settings → Connectors → Monday.com** and click **Connect Monday.com** to run
the OAuth flow.

## Auth (OAuth 2.0)

Connects via Monday's OAuth. The connect → callback → token-persist loop runs
entirely from the plugin's managed directory. Requires a Monday app's
credentials in the host environment:

| Var | Purpose |
|---|---|
| `LUNA_MONDAY_CLIENT_ID` | Monday app client ID |
| `LUNA_MONDAY_CLIENT_SECRET` | Monday app client secret |

Set the app's redirect URI to `<luna-origin>/api/p/plugin-monday/callback`.

## What it does

17 skill-gated tools across four skills:

| Skill | Tools |
|---|---|
| `monday-boards` | list/get boards, list/create groups |
| `monday-items` | list/get/create/update/delete/move/archive items |
| `monday-columns` | set status, get column values |
| `monday-updates` | create/list updates, create/list subitems |

The OAuth token is stored in Luna's vault; auth-gated REST routes live under
`/api/p/plugin-monday/*` (including a webhook receiver).

## Settings UI

Served as a themed **iframe** from the plugin's own managed directory
(`interface/webui/settings/index.html`) — ships its own UI without compiling
into Luna core's bundle. Crash-isolated and React-version immune.

## Layout

```
plugin_monday/
  __init__.py        # the plugin (luna_sdk only) — tools + skills + settings tab
  client.py          # MondayClient + exchange_code (pure httpx)
  routes.py          # OAuth connect/callback, status, disconnect, webhook + iframe UI
  state.py           # process-level MondayClient holder (OAuth hot-swap, no registry reach-in)
  interface/webui/settings/index.html   # the iframe settings page (OAuth popup)
  luna-plugin.toml   # the data manifest the marketplace reads
```

## License

MIT — see [LICENSE](./LICENSE).
