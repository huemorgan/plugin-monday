# 003 — Luna listed as an external agent inside monday.com

monday.com's external-agent API (pre-release, `API-Version: dev`;
https://developer.monday.com/api-reference/docs/build-an-external-agent) lets a
third-party service appear as an **agent** in a monday account: users chat with
it, @mention it in updates, or assign it items. monday POSTs a signed
`agent_triggered` event to a callback URL and expects the reply in the HTTP
body (chat) or an ack followed by a GraphQL-posted update (mention/assigned).

Roy's ask (2026-09-07): a button in the plugin settings that creates the agent
in the account once monday is connected, using the connect mutation; and an
answer to *which* URL monday should call given Fly machines are ephemeral.

## Callback URL: the luna-service gateway, never the machine

Hosted tenants run on Fly machines that sleep and get replaced; their host is
not a stable public endpoint. The stable one is the webhook gateway on
luna-service (luna.com.ai, plan 076): plugin-webhooks mints
`https://luna.com.ai/api/webhooks/hooks/{agent}/{slug}`, the gateway wakes the
machine, waits for readiness, relays the raw body + headers to the plugin
route (sync mode) and returns the plugin's response verbatim. Same path the
board webhooks already use since plan 001. Self-hosted Lunas mint from
`LUNA_BASE_URL`.

Known limits of that hop (to fix on the luna-service side later):
- The relay buffers the response (`Response(content=resp.content)`), so SSE
  streaming degrades to one flush at the end — monday still parses it.
- monday allows ~30 s per callback; a cold machine needs up to ~45 s to
  wake. First chat to a sleeping agent can time out; mention/assigned acks
  have the same exposure since the ack itself comes from the plugin.

## Design

- `plugin_monday/agent.py` (new): HMAC verify (`sha256=HMAC(secret,
  "{ts}.{raw}")`, 10 min skew), dedupe ring, prompt builder (user words
  first, monday's generated instruction second), SSE framing, pydantic-ai
  delta reader, `post_reply` (as the agent via its `api_token`, fallback to
  the connected user).
- `client.py`: `_gql_direct` gains per-call `api_version` + `timeout`;
  agent mutations (`connect_external_agent_sync`, `activate_agent`,
  `update_custom_agent`, `disconnect_external_agent`,
  `add_agent_resource_access`) all on `API-Version: dev`, connect with a
  60 s timeout (monday: ≥40 s). `create_update` gains `parent_id`.
  The OAuth/MCP passthrough pins its own API version — dev-only fields fail
  there; the route turns that into "reconnect with a personal API token".
- `routes.py`:
  - `POST /agent/connect {name}` → mint hook `monday-agent` (sync, target
    `/api/p/plugin-monday/agent/{secret}`) → connect → persist bundle
    (agent_id, signing_secret, api_token — shown once) → activate → persist.
  - `POST /agent/disconnect` → `disconnect_external_agent` best-effort,
    drop the bundle.
  - `POST /agent/{secret}` → path secret + HMAC + freshness; `chat` streams
    SSE (`{"type":"text","content":…}` deltas via `event_stream_handler`,
    keepalive comments while tools run, final text if nothing streamed,
    `[DONE]`); `stream:false` returns `{"message": …}`; mention/assigned
    ack `[DONE]` and run the turn in a background task, then post the
    reply in the thread (`parent_id = updateId`) as the agent.
  - `/status` gains `agent: {agent_id, name, callback_url, active}`.
- `__init__.py`: `agent_callback_url()` (mint), `set_plugin` in
  `state.py` so routes reach the plugin; new tool
  `monday_agent_grant_board_access` (agents don't inherit board access) in a
  new `monday-agent` skill. 29 tools. Version 0.7.0 (three stamps).
- Settings page: new card `LUNA INSIDE MONDAY.COM` — headline "Not listed
  yet" + name input + **Add to Monday.com**; "Listed as {name}" + **Remove**;
  "Needs the Webhooks plugin" when plugin-webhooks is missing. Detail
  behind an expander (how it works, board-access hint, callback URL).

## Verification

- Unit: helpers, client header/timeout contracts, connect/disconnect
  routes, callback auth, chat SSE, mention background reply + dedupe,
  grant tool. Existing suite green.
- Live: needs a monday account with a personal API token — not available in
  this session; the mutation shapes follow the docs verbatim and every call
  is stubbed at the transport, so the first real click is the integration
  test. Ship 0.7.0, push, publish.
