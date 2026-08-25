"""plugin-monday API routes — no-app OAuth (DCR + PKCE), token connect, status,
disconnect, webhook receiver, and the iframe settings UI."""

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
from fastapi.responses import FileResponse
from pydantic import BaseModel

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


class _TokenReq(BaseModel):
    token: str


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
                return _StatusResp(connected=False)

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

        event_type = payload.get("event", {}).get("type", "")
        bus_event = WEBHOOK_EVENT_MAP.get(event_type)

        if bus_event:
            await ctx.events.emit(bus_event, payload)
            log.info("monday webhook: %s", bus_event)
        else:
            await ctx.events.emit("monday.event", payload)
            log.info("monday webhook: unmapped type %s", event_type)

        return {"ok": True}

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
