# 003 — execution summary (v0.7.0, 2026-09-07)

Shipped: Luna can be listed as an external agent inside monday.com from the
plugin settings page.

## What landed

- `plugin_monday/agent.py` — signature/freshness check, dedupe ring, prompt
  builder, SSE framing, pydantic-ai delta reader, `post_reply` (agent
  identity → owner fallback, thread reply via `parent_id`).
- `client.py` — `API-Version: dev` + per-call timeout on `_gql_direct`;
  `connect_external_agent` (60 s), `activate_agent`, `update_custom_agent`,
  `disconnect_external_agent`, `add_agent_resource_access`;
  `create_update(parent_id=)`.
- `routes.py` — `POST /agent/connect`, `POST /agent/disconnect`,
  `POST /agent/{secret}` callback (chat → SSE / JSON; mention+assigned →
  ack then background reply); `/status.agent`.
- `__init__.py` / `state.py` — `agent_callback_url()` minted through
  plugin-webhooks (`monday-agent`, sync); `set_plugin`; new tool
  `monday_agent_grant_board_access` in skill `monday-agent`. 29 tools.
- Settings card “Luna inside Monday.com”: Add / Remove, name input,
  webhooks-plugin gate, expandable how-it-works + callback URL.
- Tests: `tests/test_external_agent.py` (21), suite 66 green. Manifest and
  probe counts bumped to 29.

## Verified

- Unit suite green; plugin imports under the real luna_sdk (luna submodule
  venv), settings JS parses.
- Not verified live: no monday account/token in this session, so the
  `connect_external_agent_sync` mutation and the callback contract were
  exercised only against transport stubs shaped from the docs. First real
  click is the integration test; the enum names for
  `add_agent_resource_access` (`AgentResourceScopeType`,
  `AgentResourcePermissionType`) are inferred and may need adjusting.

## Open caveats (luna-service side, recommendation only)

- Relay buffers the plugin response; SSE reaches monday as one flush.
- monday's ~30 s callback limit vs up to ~45 s cold wake of a sleeping
  machine: first chat after idle may time out. A monday-aware fast ack at
  the gateway, or streaming passthrough, would close both.
- OAuth/MCP passthrough cannot select `API-Version: dev`; the connect route
  turns the resulting GraphQL error into "reconnect with a personal API
  token".
