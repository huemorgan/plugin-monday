"""MondayTriggerSource — publishes the monday.* bus events as playbook triggers.

Static catalog: the receiving route (routes.py WEBHOOK_EVENT_MAP) emits these
event names for every delivery from a registered Monday webhook. Whether a
given board actually sends them depends on which webhooks the agent created
with monday_create_webhook — the trigger source advertises what CAN be bound,
per the TriggerSource contract (registry is discovery-only; playbooks
subscribe to the bus event directly).

ensure/release are no-ops: Monday-side webhooks are created and deleted with
the monday webhook tools, independently of any playbook binding.
"""

from __future__ import annotations

import logging
from typing import Any

from luna_sdk import TriggerInfo

log = logging.getLogger("plugin-monday.triggers")

# Flattened by routes.py onto the payload top level so playbook filters can
# be written as {"boardId": 123} instead of {"event.boardId": 123}.
_EXAMPLE = {
    "boardId": 1234567890,
    "itemId": 987654321,
    "type": "create_pulse",
    "event": {"...": "raw Monday webhook event, all provider fields"},
}

_FILTER_HINT = 'Filter on a single board with {"boardId": <board id>}.'

# (event_pattern, label, description) — patterns MUST stay in sync with
# routes.py WEBHOOK_EVENT_MAP values.
_CATALOG: list[tuple[str, str, str]] = [
    ("monday.item.created", "Item created", "A new item was added to a watched board."),
    ("monday.item.renamed", "Item renamed", "An item's name changed on a watched board."),
    ("monday.item.moved", "Item moved", "An item moved to another group."),
    ("monday.item.archived", "Item archived", "An item was archived."),
    ("monday.item.deleted", "Item deleted", "An item was deleted."),
    ("monday.item.restored", "Item restored", "An archived/deleted item was restored."),
    ("monday.column.changed", "Column value changed", "Any column value changed on an item."),
    ("monday.status.changed", "Status changed", "A status column changed on an item."),
    ("monday.subitem.created", "Subitem created", "A subitem was added to an item."),
    ("monday.subitem.changed", "Subitem changed", "A subitem's column value changed."),
    ("monday.update.created", "Update posted", "An update (comment) was posted on an item."),
    ("monday.update.edited", "Update edited", "An update was edited."),
    ("monday.update.deleted", "Update deleted", "An update was deleted."),
    ("monday.date.arrived", "Date arrived", "A date column's date arrived (when_date_arrived)."),
    ("monday.event", "Any other Monday event", "Catch-all for event types without a dedicated pattern."),
]


class MondayTriggerSource:
    source_name = "monday"

    async def list_triggers(self, app: str | None = None) -> list[TriggerInfo]:
        if app is not None and app != "monday":
            return []
        return [
            TriggerInfo(
                slug=pattern.replace(".", "_"),
                source=self.source_name,
                app="monday",
                label=label,
                event_pattern=pattern,
                config_schema={},
                payload_example=_EXAMPLE,
                description=(
                    f"{desc} Requires a Monday webhook on the board "
                    f"(monday_create_webhook). {_FILTER_HINT}"
                ),
            )
            for pattern, label, desc in _CATALOG
        ]

    async def ensure_trigger(self, slug: str, config: dict[str, Any]) -> str:
        """The Monday-side webhook is managed by the webhook tools — nothing
        to create here; return a stable id."""
        return f"monday:{slug}"

    async def release_trigger(self, slug: str) -> None:
        """No teardown; the Monday webhook outlives playbook bindings."""
