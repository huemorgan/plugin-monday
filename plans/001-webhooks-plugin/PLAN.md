# 001 — deliver Monday webhooks through plugin-webhooks

## Why

Monday currently registers webhooks pointing straight at the machine's own
URL (`{public_base}/api/p/plugin-monday/webhook/{secret}`). On hosted
tenants a sleeping machine misses those deliveries — Monday retries only
briefly, then the event is lost. plugin-webhooks mints gateway URLs that
wake the machine and wait for readiness before delivering.

Roy's directive (2026-08-27): Monday uses the Webhooks plugin **only** —
no direct-URL fallback. If plugin-webhooks is not installed, the agent and
the settings page must say to install it; if it is installed, they explain
that Monday events can trigger playbooks.

## Design

- **Mode is sync, not queue**: the gateway's queue path answers 202
  without reading the body, so it can never echo Monday's registration
  challenge. Sync relays the plugin's response (challenge echo works) and
  still wakes + waits + retries once on an unreachable machine.
- `_webhook_url()` → resolve the live plugin-webhooks instance via
  `plugin_webhooks.state.get_plugin()` (in-process singleton, None when
  not installed/unloaded; ImportError when never loaded).
  - Installed → `create_hook("monday-events", plugin="plugin-monday",
    target="/api/p/plugin-monday/webhook/{secret}", mode="sync")` —
    idempotent upsert keeps slug/secret stable; use the returned
    `public_url`. Monday's own receiving route is unchanged (secret in
    path stays the auth; challenge echo already there).
  - Not installed → raise a steering error: install plugin-webhooks from
    the marketplace (tool-layer refusal per flows-belong-in-tool-layer).
  - The legacy `public_base`/`LUNA_BASE_URL` direct path is removed
    (in self-hosted Lunas plugin-webhooks mints local URLs anyway).
- **Status API**: `/status` gains `webhooks_ready: bool`.
- **Settings UI**: new card, eyebrow `TRIGGERS`, headline "Triggers from
  Monday.com" — green state ("Board changes can trigger this agent…
  playbook triggers") vs. install prompt ("Install the Webhooks plugin
  from the Marketplace…"). Per vision/ux_guidelines.md.
- **Agent-facing text**: monday_create_webhook description + the
  monday-webhooks skill body state the plugin-webhooks requirement so the
  agent tells the user to install it instead of failing opaquely.
- Existing Monday webhooks registered under old direct URLs keep working
  (route untouched); recreating them moves them onto the gateway.

## Verification

- Unit tests: gate (not installed → steering error), mint path (stubbed
  plugin_webhooks returns public_url; sync mode + correct target asserted),
  status flag, existing suite green.
- QA Luna 8766 (has both plugins): /status shows webhooks_ready, settings
  page renders the triggers card in installed state; plugin-webhooks
  settings lists the minted hook as an external row once created.
- Ship: version 0.4.0 (pyproject + toml + in-code manifest), push
  huemorgan/plugin-monday, bump submodule in luna-plugins, publish to
  marketplaces.com.ai, verify catalog.
