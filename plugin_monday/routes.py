"""plugin-monday API routes — OAuth connect/callback, status, disconnect, webhook."""

import logging
import os
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
            return f"{parsed.scheme}://{parsed.netloc}"
    return str(request.base_url).rstrip("/")

log = logging.getLogger("plugin-monday.routes")

VAULT_TOKEN_KEY = "plugin_monday.oauth"
VAULT_ACCOUNT_KEY = "plugin_monday.account_id"

MONDAY_AUTHORIZE_URL = "https://auth.monday.com/oauth2/authorize"

_SETTINGS_DIR = Path(__file__).parent / "interface" / "webui" / "settings"

WEBHOOK_EVENT_MAP = {
    "create_item": "monday.item.created",
    "change_column_value": "monday.column.changed",
    "change_status_column_value": "monday.status.changed",
    "create_update": "monday.update.created",
    "create_subitem": "monday.subitem.created",
    "delete_item": "monday.item.deleted",
}


class _StatusResp(BaseModel):
    connected: bool
    account_name: str | None = None
    board_count: int | None = None


def register_routes(app, ctx):
    from luna_sdk import get_current_user

    router = APIRouter(prefix="/api/p/plugin-monday", tags=["monday"])

    def _vault():
        vault = ctx.vault
        if vault is None:
            raise HTTPException(503, "Vault not available")
        return vault

    def _client_id() -> str:
        cid = os.environ.get("LUNA_MONDAY_CLIENT_ID", "")
        if not cid:
            raise HTTPException(500, "LUNA_MONDAY_CLIENT_ID not configured")
        return cid

    def _client_secret() -> str:
        secret = os.environ.get("LUNA_MONDAY_CLIENT_SECRET", "")
        if not secret:
            raise HTTPException(500, "LUNA_MONDAY_CLIENT_SECRET not configured")
        return secret

    @router.get("/connect")
    async def connect(request: Request, user=Depends(get_current_user)):
        """Redirect the user to Monday.com OAuth consent screen."""
        from fastapi.responses import HTMLResponse, RedirectResponse

        cid = os.environ.get("LUNA_MONDAY_CLIENT_ID", "")
        if not cid:
            return HTMLResponse(
                "<html><body style='font-family:sans-serif;padding:40px;background:#1a1a2e;color:#e0e0e0'>"
                "<h2>Monday.com OAuth not configured</h2>"
                "<p>Set <code>LUNA_MONDAY_CLIENT_ID</code> and "
                "<code>LUNA_MONDAY_CLIENT_SECRET</code> environment variables to enable Monday.com integration.</p>"
                "<p><a href='https://monday.com/developers/apps' style='color:#7c5cff'>Create a Monday app →</a></p>"
                "<button onclick='window.close()' style='margin-top:16px;padding:8px 20px;border-radius:8px;"
                "background:#7c5cff;color:white;border:none;cursor:pointer;font-size:14px'>Close</button>"
                "</body></html>",
                status_code=200,
            )

        base_url = _public_base(request)
        redirect_uri = f"{base_url}/api/p/plugin-monday/callback"
        params = urllib.parse.urlencode({
            "client_id": cid,
            "redirect_uri": redirect_uri,
        })
        return RedirectResponse(f"{MONDAY_AUTHORIZE_URL}?{params}")

    @router.get("/callback")
    async def callback(request: Request, code: str = ""):
        """OAuth callback — exchange code for token, store in vault."""
        if not code:
            raise HTTPException(400, "Missing authorization code")

        from .client import MondayClient, exchange_code

        client_id = _client_id()
        client_secret = _client_secret()

        token_data = await exchange_code(client_id, client_secret, code)
        token = token_data.get("access_token")
        if not token:
            raise HTTPException(502, "No access_token in Monday response")

        vault = _vault()
        await vault.store_credential(VAULT_TOKEN_KEY, token, kind="oauth")

        client = MondayClient(token)
        try:
            account_data = await client.get_account()
            account_name = account_data.get("me", {}).get("account", {}).get("name", "")
            account_id = str(account_data.get("me", {}).get("account", {}).get("id", ""))
            if account_id:
                await vault.store_credential(VAULT_ACCOUNT_KEY, account_id, kind="metadata")
        finally:
            await client.close()

        set_client(MondayClient(token))

        await ctx.events.emit("monday.connected", {
            "account_name": account_name,
            "account_id": account_id,
        })

        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            "<html><body><script>window.close()</script>"
            "<p>Monday.com connected. You can close this tab.</p></body></html>"
        )

    @router.post("/disconnect")
    async def disconnect(user=Depends(get_current_user)):
        vault = _vault()
        try:
            await vault.delete_credential(VAULT_TOKEN_KEY)
        except KeyError:
            pass
        try:
            await vault.delete_credential(VAULT_ACCOUNT_KEY)
        except KeyError:
            pass

        client = get_client()
        if client is not None:
            await client.close()
            set_client(None)

        return {"connected": False}

    @router.get("/status", response_model=_StatusResp)
    async def status(user=Depends(get_current_user)):
        vault = _vault()
        try:
            await vault.get_credential(VAULT_TOKEN_KEY)
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
            account_name=account_name,
            board_count=board_count,
        )

    @router.post("/webhook/{secret}")
    async def webhook(request: Request, secret: str):
        """Receive Monday.com webhook events. Monday embeds the secret in the URL."""
        payload = await request.json()

        # Monday sends a challenge on first webhook registration
        if "challenge" in payload:
            return {"challenge": payload["challenge"]}

        event_type = payload.get("event", {}).get("type", "")
        bus_event = WEBHOOK_EVENT_MAP.get(event_type)

        if bus_event:
            await ctx.events.emit(bus_event, payload)
            log.info("monday webhook: %s", bus_event)
        else:
            log.info("monday webhook: unknown type %s", event_type)

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
