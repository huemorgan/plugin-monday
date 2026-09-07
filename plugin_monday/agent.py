"""Monday.com external agent — Luna listed inside monday as a custom agent.

monday calls the plugin's callback URL (minted through plugin-webhooks so
it survives machine restarts and wakes a sleeping machine) with a signed
``agent_triggered`` POST. Three trigger types:

- ``chat``     — the reply goes back in the HTTP body (SSE ``text`` events
                 then ``[DONE]``); monday shows it in the agent chat window.
- ``mention``  — ack immediately (``[DONE]``), then post the reply as an
                 update in the item's thread, acting as the agent.
- ``assigned`` — ack immediately, then post an update on the item.

Everything here is pure helpers (signature check, prompt building, SSE
framing, reply delivery) so routes.py stays thin and the pieces unit-test
without a running Luna.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any

log = logging.getLogger("plugin-monday.agent")

VAULT_AGENT_BUNDLE_KEY = "plugin_monday.external_agent"
VAULT_AGENT_CALLBACK_SECRET_KEY = "plugin_monday.agent_callback_secret"

HOOK_NAME = "monday-agent"
DEFAULT_AGENT_NAME = "Luna"
SIGNATURE_MAX_SKEW_S = 10 * 60
# monday closes the request after ~30 s; leave headroom for the relay hop.
CHAT_TURN_TIMEOUT_S = 110.0
BACKGROUND_TURN_TIMEOUT_S = 300.0
SSE_KEEPALIVE_S = 10.0


# ── signature ─────────────────────────────────────────────────


def verify_signature(signing_secret: str, timestamp: str, raw_body: bytes, header: str) -> bool:
    """HMAC-SHA256 over ``f"{timestamp}.{raw_body}"``; header is ``sha256=<hex>``."""
    if not signing_secret or not timestamp or not header:
        return False
    mac = hmac.new(signing_secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(expected, header.strip())


def timestamp_fresh(timestamp: str, *, now: float | None = None) -> bool:
    """monday sends epoch milliseconds; reject anything outside the skew window."""
    try:
        ts = float(timestamp) / 1000.0
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    return abs(now - ts) <= SIGNATURE_MAX_SKEW_S


def new_callback_secret() -> str:
    return secrets.token_urlsafe(24)


# ── dedupe ────────────────────────────────────────────────────


class RecentKeys:
    """Tiny bounded set so a monday retry doesn't run the same turn twice."""

    def __init__(self, limit: int = 256) -> None:
        self._limit = limit
        self._keys: list[str] = []

    def seen(self, key: str) -> bool:
        if key in self._keys:
            return True
        self._keys.append(key)
        if len(self._keys) > self._limit:
            del self._keys[: len(self._keys) - self._limit]
        return False


def dedupe_key(headers: dict[str, str], body: dict[str, Any]) -> str:
    payload = body.get("payload") or {}
    return "|".join(
        str(x) for x in (
            headers.get("x-monday-timestamp", ""),
            body.get("triggerType", ""),
            payload.get("updateId", ""),
            payload.get("itemId", ""),
        )
    )


# ── prompt ────────────────────────────────────────────────────


def _payload_lines(payload: dict[str, Any]) -> list[str]:
    lines = []
    for key, label in (
        ("boardId", "Board id"),
        ("itemId", "Item id"),
        ("groupId", "Group id"),
        ("updateId", "Update id"),
        ("replyId", "Reply id"),
    ):
        val = payload.get(key)
        if val not in (None, "", 0):
            lines.append(f"- {label}: {val}")
    files = payload.get("files")
    if files:
        lines.append(f"- Files attached: {json.dumps(files)[:400]}")
    return lines


def build_prompt(body: dict[str, Any]) -> str:
    """Turn a monday ``agent_triggered`` body into a headless-turn prompt.

    ``payload.updateBody`` carries the user's own words; ``payload.text`` is
    often monday's generated instruction prompt — both are given, the user's
    words first.
    """
    trigger = (body.get("triggerType") or "unknown").lower()
    payload = body.get("payload") or {}
    user_words = (payload.get("updateBody") or "").strip()
    instruction = (payload.get("text") or "").strip()

    head = {
        "chat": (
            "A monday.com user is chatting with you from inside monday.com. "
            "Answer them directly; your reply is shown as-is in their chat window. "
            "Use the Monday.com tools when the question needs board data."
        ),
        "mention": (
            "A monday.com user @mentioned you in an update (comment) on an item. "
            "Your reply will be posted as a comment in that thread. Keep it short "
            "and concrete; use the Monday.com tools to look at the item or board "
            "when needed."
        ),
        "assigned": (
            "A monday.com item was assigned to you. Do what the item asks if it is "
            "clear and within your tools; otherwise say what you found and what "
            "you need. Your reply will be posted as an update on the item."
        ),
    }.get(trigger, "A monday.com event reached you. Respond briefly.")

    parts = [head, "", f"Trigger: {trigger}"]
    parts += _payload_lines(payload)
    if user_words:
        parts += ["", "The user wrote:", user_words]
    if instruction and instruction != user_words:
        parts += ["", "monday.com's instruction:", instruction]
    parts += [
        "",
        "Reply in plain text (no markdown headings). Do not mention that you were "
        "triggered by a webhook.",
    ]
    return "\n".join(parts)


# ── SSE framing ───────────────────────────────────────────────


def sse_text(content: str) -> bytes:
    return b"data: " + json.dumps({"type": "text", "content": content}).encode() + b"\n\n"


SSE_DONE = b"data: [DONE]\n\n"
SSE_KEEPALIVE = b": keepalive\n\n"


def turn_text(result: Any) -> str:
    """Final reply text from a run_turn result, or '' for error/aborted shapes."""
    if isinstance(result, dict):
        if result.get("error") or result.get("_aborted"):
            return ""
        raw = result.get("_raw")
        return str(raw).strip() if raw else json.dumps(result)
    return (result or "").strip() if isinstance(result, str) else ""


def delta_text(event: Any) -> str | None:
    """Text carried by a pydantic-ai stream event, or None for non-text events."""
    kind = getattr(event, "event_kind", "")
    if kind == "part_start":
        part = getattr(event, "part", None)
        if getattr(part, "part_kind", "") == "text":
            return getattr(part, "content", "") or None
        return None
    if kind == "part_delta":
        delta = getattr(event, "delta", None)
        if getattr(delta, "part_delta_kind", "") == "text":
            return getattr(delta, "content_delta", "") or None
    return None


# ── reply delivery (mention / assigned) ───────────────────────


async def post_reply(
    *,
    text: str,
    payload: dict[str, Any],
    agent_client: Any | None,
    owner_client: Any | None,
) -> dict[str, Any]:
    """Post ``text`` as an update on the triggering item.

    Acts as the monday agent when its token works (the reply shows under the
    agent's name); falls back to the connected user's client, which needs no
    per-board grant. Mentions reply inside the originating thread.
    """
    item_id = payload.get("itemId")
    if not item_id or not text:
        return {"posted": False, "reason": "nothing to post"}
    parent_id = payload.get("updateId") or None
    last_error: Exception | None = None
    for who, client in (("agent", agent_client), ("owner", owner_client)):
        if client is None:
            continue
        try:
            update = await client.create_update(int(item_id), text, parent_id=parent_id)
            return {"posted": True, "as": who, "update_id": update.get("id")}
        except Exception as exc:  # noqa: BLE001 — try the next identity
            last_error = exc
            log.warning("plugin-monday agent: reply as %s failed: %s", who, exc)
    return {"posted": False, "reason": str(last_error) if last_error else "no client"}
