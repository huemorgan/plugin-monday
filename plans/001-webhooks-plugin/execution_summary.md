# Execution summary — 001 deliver Monday webhooks through plugin-webhooks

Date: 2026-08-27. Shipped as **plugin-monday 0.4.0**, commit `ed1a99a` on
huemorgan/plugin-monday main, published to marketplaces.com.ai (official
catalog `latest_version: 0.4.0`).

## What shipped

- `plugin_monday/__init__.py`
  - `find_webhooks_plugin()` — resolves the live plugin-webhooks instance.
    Checks module names `plugin_webhooks` (in-tree natural import) and
    `luna_plugin_plugin_webhooks` (managed/marketplace install; the loader's
    `_module_name_for()` prefixes `luna_plugin_`), plus a sys.modules
    suffix scan as a guard against future naming changes.
  - `_webhook_url()` rewritten: mints a **sync-mode** gateway hook via
    `create_hook("monday-events", target="/api/p/plugin-monday/webhook/{secret}",
    mode="sync", plugin="plugin-monday")` and returns its `public_url`.
    The legacy `public_base`/`LUNA_BASE_URL` direct URL path is removed —
    no fallback. When plugin-webhooks is absent, raises a steering
    RuntimeError telling the agent to have the user install it from the
    Marketplace (tool-layer refusal per flows-belong-in-tool-layer).
  - `monday_create_webhook` tool description and the `monday-webhooks`
    skill state the plugin-webhooks requirement so the agent explains the
    install instead of failing opaquely.
- `plugin_monday/routes.py` — `/status` gains `webhooks_ready: bool`,
  computed by delegating to `find_webhooks_plugin()`.
- `interface/webui/settings/index.html` — new card, eyebrow
  **TRIGGERS FROM MONDAY.COM**:
  - ready: green dot, "Board changes can trigger this agent", support text
    about monday.* playbook triggers delivered through the Webhooks plugin.
  - missing: hollow dot, "Needs the Webhooks plugin", install prompt.
- Version stamps: in-code manifest, `luna-plugin.toml`, `pyproject.toml`
  all 0.4.0.
- Tests: `tests/test_webhooks_delivery.py` (6 new — gate raises with
  Marketplace steer, loaded-but-None raises, mint contract asserts
  name/plugin/mode=sync/target, secret reused across mints, status flag
  both ways). Suite: **29 passed**.

## Why sync mode (load-bearing)

Monday's webhook registration POSTs `{"challenge": ...}` and requires it
echoed in the response. The gateway's queue path answers 202 without
reading the body, so it can never echo — queue mode would break
registration entirely. Sync mode relays the plugin's response (challenge
echo works) and still wakes a sleeping machine, waits for readiness, and
retries once on connect failure. Deliveries during machine downtime are
not queued for Monday hooks; the wake+retry path is the reliability win.

## Verification

- Unit: 29 passed (`.venv/bin/python -m pytest tests -q`).
- Real Luna (QA 8766, both plugins in `LUNA_MANAGED_DIR`):
  `/api/p/plugin-monday/status` → `"webhooks_ready": true`.
- Settings page screenshots via CDP (scratchpad
  `qa-monday-triggers-ready.png` / `qa-monday-triggers-missing.png`):
  both card states render per ux_guidelines (eyebrow → headline → support).

## Surprises / learnings

- **Loader module naming**: a plain
  `from plugin_webhooks.state import get_plugin` returned False on real
  QA despite the plugin being installed — the loader imports managed
  plugins as `luna_plugin_plugin_webhooks`
  (`luna/plugins/loader.py:171-173`), natural names only for in-tree
  plugins. Unit tests (which stub `sys.modules["plugin_webhooks"]`) could
  not catch this; the real-Luna check did. Cross-plugin lookups must scan
  both names (now centralized in `find_webhooks_plugin`).
- `/api/plugins` on QA returns a bare list, not `{"plugins": [...]}`.

## Deviations from PLAN.md

None material. The plan's "resolve via `plugin_webhooks.state.get_plugin()`"
became the dual-name scan above for the reason stated.

## Follow-ups

- Existing Monday webhooks registered under old direct URLs keep working
  (receiving route unchanged); recreating a webhook moves it onto the
  gateway. Installed agents pick this up via marketplace upgrade.
