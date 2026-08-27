# Execution summary — 002 playbook trigger discovery + flat filter payloads

Date: 2026-08-27. Shipped as **plugin-monday 0.5.0**, pushed to
huemorgan/plugin-monday main, published to marketplaces.com.ai
(`latest_version: 0.5.0`). Follows 0.4.x (webhooks via plugin-webhooks).

## Why

A production agent debugging "webhook fired but playbook didn't run" hit two
plugin gaps:

1. `playbook_list_available_triggers` returned nothing for Monday — the
   plugin never registered its events in Luna's trigger registry
   (`ctx.trigger_sources`, luna 006.713). The registry is discovery-only
   (playbooks subscribe to bus events directly), but the empty list sent the
   agent down a wrong diagnosis.
2. The actual non-firing cause: the route emitted Monday's raw payload, where
   ids are nested (`event.boardId`), while the playbook filter matcher does
   flat dot-path equality — a natural filter `{"boardId": ...}` never
   matched, and runs were silently skipped.

## What shipped

- `plugin_monday/triggers.py` (new): `MondayTriggerSource` advertising a
  static catalog of 15 events — every `WEBHOOK_EVENT_MAP` value plus the
  `monday.event` catch-all. `ensure/release` are no-ops (Monday-side
  webhooks are managed by the webhook tools, independent of bindings).
- `__init__.py` on_load registers it in `ctx.trigger_sources`
  (feature-detected + try/except — older cores degrade silently);
  on_unload unregisters.
- `routes.py` webhook route: flattens `boardId`, `pulseId` (also copied to
  `itemId`), `groupId`, `columnId`, `userId`, `type` onto the payload top
  level; the raw Monday event stays nested under `event`.
- `monday-webhooks` skill documents the filter shape
  (`{"boardId": <board id>}`).
- Tests: `tests/test_triggers.py` (5 — catalog covers every emitted event,
  app filter, flattening for mapped and unmapped types, challenge echo
  unchanged); conftest luna_sdk stub gained `TriggerInfo`. Suite: **34
  passed**.

## Verification

- Unit: 34 passed.
- Real Luna (QA 8766): temporary `qa-trigger-probe` fixture plugin exposed
  `ctx.trigger_sources.all_triggers()` over HTTP — returned the full monday
  catalog (registry is exactly what `playbook_list_available_triggers`
  reads). Fixture removed after the check.

## Production follow-up

The affected tenant's playbook filter `{"boardId": 5099347389}` becomes
correct as-is once the plugin is upgraded to 0.5.0 — the agent was prompted
to upgrade, confirm trigger discovery, re-save playbooks (resync), and test
end-to-end with a throwaway item. Existing Monday webhooks are untouched.
