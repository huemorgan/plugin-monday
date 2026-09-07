"""Probe contract: shared auth probe attached to every tool, failure classes mapped."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from plugin_monday import MondayPlugin
from plugin_monday.client import MondayAPIError
from plugin_monday.state import set_client


@pytest.fixture(autouse=True)
def _reset_client():
    set_client(None)
    yield
    set_client(None)


class _FakeClient:
    def __init__(self, result: Any = None, exc: Exception | None = None):
        self._result = result
        self._exc = exc

    async def api(self, query: str, variables: dict | None = None) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._result


def _http_error(status: int, url: str = "https://api.monday.com/v2", body: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", url)
    resp = httpx.Response(status, text=body, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


def _probe(plugin: MondayPlugin) -> dict:
    return asyncio.run(plugin.probe_auth())


@pytest.fixture
def plugin() -> MondayPlugin:
    return MondayPlugin()


def test_not_connected_is_credential_dead(plugin):
    result = _probe(plugin)
    assert result["ok"] is False
    assert result["failure_class"] == "credential_dead"
    assert "not connected" in result["detail"]


def test_ok_with_identity(plugin):
    set_client(_FakeClient(result={"me": {"id": "1", "name": "Roy"}}))
    result = _probe(plugin)
    assert result["ok"] is True
    assert result["failure_class"] is None
    assert "Roy" in result["detail"]


def test_http_401_is_credential_dead(plugin):
    set_client(_FakeClient(exc=_http_error(401)))
    assert _probe(plugin)["failure_class"] == "credential_dead"


def test_http_402_and_403_are_permission(plugin):
    for status in (402, 403):
        set_client(_FakeClient(exc=_http_error(status)))
        assert _probe(plugin)["failure_class"] == "permission"


def test_http_429_is_rate_limited(plugin):
    set_client(_FakeClient(exc=_http_error(429)))
    assert _probe(plugin)["failure_class"] == "rate_limited"


def test_html_502_is_unknown_with_snippet(plugin):
    set_client(_FakeClient(exc=_http_error(502, body="<html><body>Bad Gateway</body></html>")))
    result = _probe(plugin)
    assert result["failure_class"] == "unknown"
    assert "502" in result["detail"]


def test_oauth_refresh_failure_is_credential_dead(plugin):
    set_client(_FakeClient(exc=_http_error(400, url="https://auth.monday.com/oauth2/token")))
    result = _probe(plugin)
    assert result["failure_class"] == "credential_dead"
    assert "refresh" in result["detail"].lower()


def test_gql_unauthorized_is_credential_dead(plugin):
    set_client(_FakeClient(exc=MondayAPIError("User unauthorized to perform action")))
    assert _probe(plugin)["failure_class"] == "credential_dead"


def test_gql_complexity_is_rate_limited(plugin):
    set_client(_FakeClient(exc=MondayAPIError("Complexity budget exhausted")))
    assert _probe(plugin)["failure_class"] == "rate_limited"


def test_network_error_is_unknown(plugin):
    req = httpx.Request("POST", "https://api.monday.com/v2")
    set_client(_FakeClient(exc=httpx.ConnectError("boom", request=req)))
    assert _probe(plugin)["failure_class"] == "unknown"


def test_probe_attached_to_all_tools(plugin):
    registered: list = []

    class _Registry:
        def register(self, owner, tool_def, handler, skill_gated=False):
            registered.append(tool_def)

    class _Ctx:
        tool_registry = _Registry()

    plugin._register_tools(_Ctx())
    assert len(registered) == 29
    for td in registered:
        assert td.probe is not None
        assert td.probe.kind == "auth"
        assert td.probe.handler is not None
    # one shared probe object, not 28
    assert len({id(td.probe) for td in registered}) == 1
