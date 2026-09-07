"""plugin-monday API routes — no-app OAuth (DCR + PKCE), token connect, status,
disconnect, webhook receiver, and the iframe settings UI."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import agent as agent_mod
from .state import get_client, set_client


def _public_base(request: Request) -> str:
    env = os.environ.get("LUNA_BASE_URL", "").rstrip("/")
    if env:
        return env
    for hdr in ("referer", "origin"):
        val = request.headers.get(hdr)
        if val:
            parsed = urlparse(val)
            # Hosted tenants live under a path prefix (/a/{slug}) the plugin
            # app never sees — recover it from the referer path.
            prefix = ""
            if "/api/p/plugin-monday" in parsed.path:
                prefix = parsed.path.split("/api/p/plugin-monday", 1)[0].rstrip("/")
            return f"{parsed.scheme}://{parsed.netloc}{prefix}"
    return str(request.base_url).rstrip("/")

log = logging.getLogger("plugin-monday.routes")

VAULT_TOKEN_KEY = "plugin_monday.oauth"
VAULT_ACCOUNT_KEY = "plugin_monday.account_id"
VAULT_OAUTH_BUNDLE_KEY = "plugin_monday.oauth2"
VAULT_DCR_CLIENT_KEY = "plugin_monday.dcr_client"
VAULT_OAUTH_PENDING_KEY = "plugin_monday.oauth_pending"
VAULT_WEBHOOK_SECRET_KEY = "plugin_monday.webhook_secret"

_SETTINGS_DIR = Path(__file__).parent / "interface" / "webui" / "settings"

WEBHOOK_EVENT_MAP = {
    "create_pulse": "monday.item.created",
    "create_item": "monday.item.created",
    "update_column_value": "monday.column.changed",
    "change_column_value": "monday.column.changed",
    "change_specific_column_value": "monday.column.changed",
    "change_status_column_value": "monday.status.changed",
    "update_name": "monday.item.renamed",
    "change_name": "monday.item.renamed",
    "create_update": "monday.update.created",
    "edit_update": "monday.update.edited",
    "delete_update": "monday.update.deleted",
    "create_subitem": "monday.subitem.created",
    "change_subitem_column_value": "monday.subitem.changed",
    "delete_pulse": "monday.item.deleted",
    "item_deleted": "monday.item.deleted",
    "item_archived": "monday.item.archived",
    "item_restored": "monday.item.restored",
    "item_moved_to_any_group": "monday.item.moved",
    "item_moved_to_specific_group": "monday.item.moved",
    "when_date_arrived": "monday.date.arrived",
}


class _StatusResp(BaseModel):
    connected: bool
    method: str | None = None
    account_name: str | None = None
    board_count: int | None = None
    # True when plugin-webhooks is installed — Monday triggers deliver
    # through it and are unavailable without it.
    webhooks_ready: bool = False
    # Luna listed as a custom agent inside monday (external agent API).
    agent: dict | None = None


class _AgentConnectReq(BaseModel):
    name: str | None = None


def _webhooks_ready() -> bool:
    from . import find_webhooks_plugin

    return find_webhooks_plugin() is not None


class _TokenReq(BaseModel):
    token: str


def _agent_error_text(exc: Exception, client) -> str:
    """monday's reason, plus the one steer that matters: the OAuth passthrough
    can't reach the pre-release agent API — a personal API token can."""
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if getattr(client, "transport", "") == "oauth" and (
        "cannot query field" in low or "unknown" in low or "not found" in low
        or "version" in low or "isError" in msg
    ):
        return (
            "monday.com's OAuth connection can't reach the external-agent API yet. "
            "Disconnect and connect again with a personal API token "
            "(monday.com → avatar → Developers → My access tokens), then retry. "
            f"({msg[:200]})"
        )
    return msg[:400]


class _AgentIdentityClient:
    """MondayClient bound to the agent's own token: every call carries the
    pre-release API version and closes its transport after use."""

    def __init__(self, client, api_version: str) -> None:
        self._client = client
        self._version = api_version

    async def create_update(self, item_id: int, body: str, *, parent_id=None) -> dict:
        try:
            if parent_id:
                q = "mutation($item:ID!, $body:String!, $parent:ID){create_update(item_id:$item, body:$body, parent_id:$parent){id}}"
                data = await self._client._gql(
                    q, {"item": item_id, "body": body, "parent": str(parent_id)},
                    api_version=self._version,
                )
            else:
                q = "mutation($item:ID!, $body:String!){create_update(item_id:$item, body:$body){id}}"
                data = await self._client._gql(q, {"item": item_id, "body": body}, api_version=self._version)
            return data.get("create_update", {}) or {}
        finally:
            await self._client.close()


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def register_routes(app, ctx):
    from luna_sdk import get_current_user

    router = APIRouter(prefix="/api/p/plugin-monday", tags=["monday"])

    def _vault():
        vault = ctx.vault
        if vault is None:
            raise HTTPException(503, "Vault not available")
        return vault

    async def _vault_json(key: str) -> dict | None:
        try:
            raw = (await _vault().get_credential(key)).value
        except KeyError:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    async def _persist_bundle(bundle: dict) -> None:
        await _vault().store_credential(
            VAULT_OAUTH_BUNDLE_KEY, json.dumps(bundle), kind="oauth",
        )

    @router.get("/connect")
    async def connect(request: Request, user=Depends(get_current_user)):
        """Start the no-app OAuth flow: self-register a client with monday
        (Dynamic Client Registration — nothing to install), then redirect the
        popup to monday's consent screen with PKCE + state."""
        from fastapi.responses import RedirectResponse

        from .client import MCP_AUTHORIZE_URL, dcr_register

        vault = _vault()
        public_base = _public_base(request)
        redirect_uri = f"{public_base}/api/p/plugin-monday/callback"

        # One registered client per redirect_uri; re-register if the Luna
        # origin changed since last time.
        dcr = await _vault_json(VAULT_DCR_CLIENT_KEY)
        if not dcr or dcr.get("redirect_uri") != redirect_uri:
            reg = await dcr_register(redirect_uri, client_name="Luna")
            dcr = {"client_id": reg["client_id"], "redirect_uri": redirect_uri}
            await vault.store_credential(
                VAULT_DCR_CLIENT_KEY, json.dumps(dcr), kind="metadata",
            )

        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        await vault.store_credential(VAULT_OAUTH_PENDING_KEY, json.dumps({
            "state": state,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "public_base": public_base,
            "client_id": dcr["client_id"],
        }), kind="metadata")

        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": dcr["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return RedirectResponse(f"{MCP_AUTHORIZE_URL}?{params}")

    @router.get("/callback")
    async def callback(request: Request, code: str = "", state: str = ""):
        """OAuth callback — verify state, exchange code (PKCE), store tokens."""
        from fastapi.responses import HTMLResponse

        from .client import client_from_bundle, dcr_exchange_code

        if not code:
            raise HTTPException(400, "Missing authorization code")

        vault = _vault()
        pending = await _vault_json(VAULT_OAUTH_PENDING_KEY)
        if not pending or not state or pending.get("state") != state:
            raise HTTPException(400, "OAuth state mismatch — restart the connect flow")
        try:
            await vault.delete_credential(VAULT_OAUTH_PENDING_KEY)
        except KeyError:
            pass

        tok = await dcr_exchange_code(
            pending["client_id"], code, pending["redirect_uri"], pending["verifier"],
        )
        if not tok.get("access_token"):
            raise HTTPException(502, "No access_token in monday response")

        bundle = {
            "client_id": pending["client_id"],
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token", ""),
            "expires_at": time.time() + float(tok.get("expires_in", 3600)),
            "public_base": pending["public_base"],
        }
        await _persist_bundle(bundle)

        client = client_from_bundle(bundle, on_refresh=_persist_bundle)
        account_name = ""
        account_id = ""
        try:
            account_data = await client.get_account()
            account = account_data.get("me", {}).get("account", {}) or {}
            account_name = account.get("name", "")
            account_id = str(account.get("id", "") or "")
            if account_id:
                await vault.store_credential(VAULT_ACCOUNT_KEY, account_id, kind="metadata")
        except Exception as exc:  # noqa: BLE001 — connected even if probe fails
            log.warning("plugin-monday: post-connect account probe failed: %s", exc)

        old = get_client()
        if old is not None:
            await old.close()
        set_client(client)

        await ctx.events.emit("monday.connected", {
            "account_name": account_name,
            "account_id": account_id,
        })

        return HTMLResponse(
            "<html><body style='font-family:sans-serif;background:#0b0e14;color:#e6e9f2;"
            "display:flex;align-items:center;justify-content:center;height:100vh'>"
            "<p>Monday.com connected — you can close this window.</p>"
            "<script>try{if(window.opener){window.opener.postMessage('monday-connected','*')}}catch(e){}"
            "window.close()</script></body></html>"
        )

    @router.post("/connect-token")
    async def connect_token(body: _TokenReq, user=Depends(get_current_user)):
        """Connect with a personal API token (monday.com → Developers → My
        access tokens) — fallback path, talks to the API directly."""
        from .client import MondayClient

        token = body.token.strip()
        if not token:
            raise HTTPException(400, "Missing token")

        probe = MondayClient(token)
        try:
            account_data = await probe.get_account()
        except Exception:
            await probe.close()
            raise HTTPException(401, "monday.com rejected the token")

        account = account_data.get("me", {}).get("account", {}) or {}
        account_name = account.get("name", "")
        account_id = str(account.get("id", "") or "")

        vault = _vault()
        await vault.store_credential(VAULT_TOKEN_KEY, token, kind="api_key")
        if account_id:
            await vault.store_credential(VAULT_ACCOUNT_KEY, account_id, kind="metadata")

        old = get_client()
        if old is not None:
            await old.close()
        set_client(probe)

        await ctx.events.emit("monday.connected", {
            "account_name": account_name,
            "account_id": account_id,
        })
        return {"connected": True, "account_name": account_name}

    @router.post("/disconnect")
    async def disconnect(user=Depends(get_current_user)):
        vault = _vault()
        for key in (VAULT_OAUTH_BUNDLE_KEY, VAULT_TOKEN_KEY, VAULT_ACCOUNT_KEY,
                    VAULT_OAUTH_PENDING_KEY):
            try:
                await vault.delete_credential(key)
            except KeyError:
                pass

        client = get_client()
        if client is not None:
            await client.close()
            set_client(None)

        return {"connected": False}

    @router.get("/status", response_model=_StatusResp)
    async def status(user=Depends(get_current_user)):
        method = None
        if await _vault_json(VAULT_OAUTH_BUNDLE_KEY):
            method = "oauth"
        else:
            try:
                await _vault().get_credential(VAULT_TOKEN_KEY)
                method = "token"
            except KeyError:
                return _StatusResp(connected=False, webhooks_ready=_webhooks_ready())

        client = get_client()
        account_name = None
        board_count = None
        if client is not None:
            try:
                acct = await client.get_account()
                account_name = acct.get("me", {}).get("account", {}).get("name")
                boards = await client.list_boards(limit=500)
                board_count = len(boards)
            except Exception:
                pass

        return _StatusResp(
            connected=True,
            method=method,
            account_name=account_name,
            board_count=board_count,
            webhooks_ready=_webhooks_ready(),
            agent=_agent_public(await _vault_json(agent_mod.VAULT_AGENT_BUNDLE_KEY)),
        )

    @router.post("/webhook/{secret}")
    async def webhook(request: Request, secret: str):
        """Receive Monday.com webhook events. The per-install secret is
        embedded in the URL our webhook tools register with monday."""
        expected = None
        try:
            expected = (await _vault().get_credential(VAULT_WEBHOOK_SECRET_KEY)).value
        except (KeyError, HTTPException):
            pass
        if expected and not secrets.compare_digest(secret, expected):
            raise HTTPException(403, "unknown webhook secret")

        payload = await request.json()

        # Monday sends a challenge on webhook registration
        if "challenge" in payload:
            return {"challenge": payload["challenge"]}

        event = payload.get("event", {})
        event_type = event.get("type", "") if isinstance(event, dict) else ""
        bus_event = WEBHOOK_EVENT_MAP.get(event_type)

        # Flatten common identifiers to the top level so playbook trigger
        # filters can be written as {"boardId": 123} — the filter matcher
        # does flat dot-path equality against this payload. The raw Monday
        # event stays nested under "event".
        bus_payload = dict(payload)
        if isinstance(event, dict):
            for key in ("boardId", "pulseId", "groupId", "columnId", "userId", "type"):
                if key in event and key not in bus_payload:
                    bus_payload[key] = event[key]
            if "pulseId" in event:
                bus_payload.setdefault("itemId", event["pulseId"])

        if bus_event:
            await ctx.events.emit(bus_event, bus_payload)
            log.info("monday webhook: %s", bus_event)
        else:
            await ctx.events.emit("monday.event", bus_payload)
            log.info("monday webhook: unmapped type %s", event_type)

        return {"ok": True}


    # --- External agent: Luna listed inside monday.com ---

    def _agent_public(bundle: dict | None) -> dict | None:
        """Bundle minus secrets — what the settings page sees."""
        if not bundle:
            return None
        return {
            "agent_id": bundle.get("agent_id"),
            "name": bundle.get("name"),
            "callback_url": bundle.get("callback_url"),
            "active": bool(bundle.get("active")),
            "created_at": bundle.get("created_at"),
        }

    def _plugin():
        from .state import get_plugin

        return get_plugin()

    async def _agent_client(bundle: dict | None):
        """A client acting AS the monday agent (its own api_token), or None."""
        from .client import AGENT_API_VERSION, MondayClient

        token = (bundle or {}).get("api_token")
        if not token:
            return None
        return _AgentIdentityClient(MondayClient(token), AGENT_API_VERSION)

    @router.post("/agent/connect")
    async def agent_connect(body: _AgentConnectReq, user=Depends(get_current_user)):
        """Create Luna as a custom agent in the connected monday account:
        mint the stable callback URL, connect_external_agent_sync (~25 s),
        activate, store the once-only secrets."""
        client = get_client()
        if client is None:
            raise HTTPException(409, "Connect Monday.com first")
        vault = _vault()
        existing = await _vault_json(agent_mod.VAULT_AGENT_BUNDLE_KEY)
        if existing and existing.get("agent_id"):
            raise HTTPException(409, "Already listed in monday.com — remove it first to recreate")

        plugin = _plugin()
        if plugin is None:
            raise HTTPException(503, "plugin not loaded")
        try:
            callback_url = await plugin.agent_callback_url()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

        name = (body.name or "").strip() or agent_mod.DEFAULT_AGENT_NAME
        try:
            created = await client.connect_external_agent(name, callback_url)
        except Exception as exc:  # noqa: BLE001 — surface monday's reason verbatim
            raise HTTPException(502, _agent_error_text(exc, client))
        agent_id = created.get("agent_id")
        if not agent_id:
            raise HTTPException(502, "monday.com returned no agent id")

        bundle = {
            "agent_id": str(agent_id),
            "name": name,
            "callback_url": callback_url,
            "signing_secret": created.get("signing_secret") or "",
            "api_token": created.get("api_token") or "",
            "instructions": created.get("instructions") or "",
            "active": False,
            "created_at": time.time(),
        }
        # Persist BEFORE activating: the secrets are shown once and a failed
        # activate must not lose them.
        await vault.store_credential(
            agent_mod.VAULT_AGENT_BUNDLE_KEY, json.dumps(bundle), kind="oauth",
        )
        activate_error = None
        try:
            res = await client.activate_agent(agent_id)
            bundle["active"] = bool(res.get("success", True))
        except Exception as exc:  # noqa: BLE001
            activate_error = _agent_error_text(exc, client)
            log.warning("plugin-monday: activate_agent failed: %s", exc)
        await vault.store_credential(
            agent_mod.VAULT_AGENT_BUNDLE_KEY, json.dumps(bundle), kind="oauth",
        )
        await ctx.events.emit("monday.agent.connected", {
            "agent_id": bundle["agent_id"], "name": name, "callback_url": callback_url,
        })
        out = {"agent": _agent_public(bundle)}
        if activate_error:
            out["warning"] = f"Created but not activated: {activate_error}"
        return out

    @router.post("/agent/disconnect")
    async def agent_disconnect(user=Depends(get_current_user)):
        vault = _vault()
        bundle = await _vault_json(agent_mod.VAULT_AGENT_BUNDLE_KEY)
        client = get_client()
        removed_remote = False
        if bundle and bundle.get("agent_id") and client is not None:
            try:
                res = await client.disconnect_external_agent(bundle["agent_id"])
                removed_remote = bool(res.get("success", True))
            except Exception as exc:  # noqa: BLE001 — local cleanup still proceeds
                log.warning("plugin-monday: disconnect_external_agent failed: %s", exc)
        try:
            await vault.delete_credential(agent_mod.VAULT_AGENT_BUNDLE_KEY)
        except KeyError:
            pass
        return {"agent": None, "removed_in_monday": removed_remote}

    @router.post("/agent/{secret}")
    async def agent_callback(request: Request, secret: str):
        """monday → Luna. Signed ``agent_triggered`` POST; chat answers in-body
        (SSE), mention/assigned ack then reply as an update in the background."""
        raw = await request.body()
        bundle = await _vault_json(agent_mod.VAULT_AGENT_BUNDLE_KEY)
        if not bundle:
            raise HTTPException(404, "no agent configured")
        expected = None
        try:
            expected = (await _vault().get_credential(
                agent_mod.VAULT_AGENT_CALLBACK_SECRET_KEY
            )).value
        except (KeyError, HTTPException):
            pass
        if expected and not secrets.compare_digest(secret, expected):
            raise HTTPException(403, "unknown callback secret")
        ts = request.headers.get("x-monday-timestamp", "")
        sig = request.headers.get("x-monday-signature", "")
        if not agent_mod.verify_signature(bundle.get("signing_secret", ""), ts, raw, sig):
            raise HTTPException(403, "bad signature")
        if not agent_mod.timestamp_fresh(ts):
            raise HTTPException(403, "stale timestamp")

        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        if body.get("event") != "agent_triggered":
            return {"message": ""}

        agent = getattr(ctx, "agent", None)
        if agent is None:
            raise HTTPException(503, "agent not ready")
        trigger = (body.get("triggerType") or "unknown").lower()
        payload = body.get("payload") or {}
        prompt = agent_mod.build_prompt(body)
        log.info("monday agent trigger: %s item=%s", trigger, payload.get("itemId"))

        if trigger == "chat":
            if body.get("stream") is False:
                text = await _run_turn(agent, prompt, timeout=agent_mod.CHAT_TURN_TIMEOUT_S)
                return {"message": text}
            return StreamingResponse(
                _stream_turn(agent, prompt),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        # mention / assigned / unknown: ack now, reply through GraphQL later.
        key = agent_mod.dedupe_key(dict(request.headers), body)
        if not _recent.seen(key):
            asyncio.create_task(_reply_later(agent, prompt, payload, bundle))
        return StreamingResponse(
            iter([agent_mod.SSE_DONE]), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    async def _run_turn(agent, prompt: str, *, timeout: float, on_event=None) -> str:
        kwargs = {"memory_read": True, "memory_write": True, "timeout_s": timeout}
        if on_event is not None:
            kwargs["event_stream_handler"] = on_event
        try:
            result, _usage = await agent.run_turn(prompt, **kwargs)
        except TypeError as exc:
            # Older cores without event_stream_handler/timeout_s kwargs.
            if "unexpected keyword" not in str(exc):
                raise
            result, _usage = await agent.run_turn(prompt, memory_read=True, memory_write=True)
        return agent_mod.turn_text(result)

    async def _stream_turn(agent, prompt: str):
        """SSE body: text deltas as the model produces them, keepalives while
        tools run, the final text if nothing streamed, then [DONE]."""
        queue: asyncio.Queue = asyncio.Queue()
        streamed = {"chars": 0}

        async def on_event(_ctx, events):
            async for ev in events:
                piece = agent_mod.delta_text(ev)
                if piece:
                    streamed["chars"] += len(piece)
                    await queue.put(piece)

        async def run():
            try:
                return await _run_turn(
                    agent, prompt, timeout=agent_mod.CHAT_TURN_TIMEOUT_S, on_event=on_event,
                )
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=agent_mod.SSE_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield agent_mod.SSE_KEEPALIVE
                    continue
                if item is None:
                    break
                yield agent_mod.sse_text(item)
            try:
                final = await task
            except Exception as exc:  # noqa: BLE001
                log.exception("monday agent chat turn failed")
                final = "" if streamed["chars"] else f"Sorry — I hit an error: {exc}"[:500]
            if not streamed["chars"]:
                yield agent_mod.sse_text(final or "I couldn't produce a reply this time.")
            yield agent_mod.SSE_DONE
        finally:
            if not task.done():
                task.cancel()

    async def _reply_later(agent, prompt: str, payload: dict, bundle: dict) -> None:
        try:
            text = await _run_turn(agent, prompt, timeout=agent_mod.BACKGROUND_TURN_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            log.exception("monday agent background turn failed")
            return
        if not text:
            log.info("monday agent: turn produced no text, nothing posted")
            return
        res = await agent_mod.post_reply(
            text=text, payload=payload,
            agent_client=await _agent_client(bundle), owner_client=get_client(),
        )
        log.info("monday agent reply: %s", res)

    _recent = agent_mod.RecentKeys()

    # --- Settings UI (served as a themed iframe by the host) ---

    @router.get("/ui/settings/")
    async def settings_index():
        index = _SETTINGS_DIR / "index.html"
        if not index.exists():
            raise HTTPException(404, "settings UI not found")
        return FileResponse(str(index), headers={"Cache-Control": "no-cache"})

    @router.get("/ui/settings/{path:path}")
    async def settings_asset(path: str):
        target = (_SETTINGS_DIR / path).resolve()
        if not str(target).startswith(str(_SETTINGS_DIR.resolve())):
            raise HTTPException(403, "forbidden")
        if not target.exists() or target.is_dir():
            return FileResponse(str(_SETTINGS_DIR / "index.html"), headers={"Cache-Control": "no-cache"})
        return FileResponse(str(target), headers={"Cache-Control": "no-cache"})

    app.include_router(router)
