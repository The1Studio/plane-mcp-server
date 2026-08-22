"""Tests for `get_work_settings` / `set_work_settings` (The1Studio fork feature).

Both call the fork's `GET|PUT /api/v1/workspaces/<slug>/work-settings/` via the
shared `_send` helper in `plane_mcp/tools/workload.py` — the plane-sdk has no
methods for either endpoint (same pattern as the rest of that module).

Response/request shape and permission model verified directly against
`The1Studio/plane@company-main`'s `apps/api/plane/workload/{api_views,views,
serializers,constants}.py` (2026-08-22): the settings field is
`max_daily_hours` (a per-day cap, defaulting to 8.0/Mon-Fri/Monday), NOT the
`max_weekly_hours` field #13's original report named — that key was renamed
before merge (`plans/260822-workload-daily-hours`) and the old key is now
explicitly REJECTED server-side, not silently aliased. All HTTP is mocked
with real `httpx.Response` objects; nothing here calls a live server.
"""

import asyncio

import httpx
import pytest
from fastmcp import FastMCP

from plane_mcp.client import PlaneClientContext
from plane_mcp.tools.workload import register_workload_tools

_REQUEST = httpx.Request("GET", "https://plane.the1studio.org/api/v1/")


class _FakeConfig:
    """Mimics the subset of plane-sdk's client.config that `_send` reads."""

    def __init__(self):
        self.base_path = "https://plane.the1studio.org/api/v1"
        self.api_key = "test-api-key"
        self.access_token = None


class _FakeClient:
    def __init__(self):
        self.config = _FakeConfig()


def _get_tool_fn(mcp: FastMCP, name: str):
    """Fetch a registered tool's underlying (sync) callable.

    `FastMCP.get_tool` is a coroutine; the returned `FunctionTool.fn` is the
    original sync function, so only the lookup needs an event loop.
    """
    tool = asyncio.run(mcp.get_tool(name))
    return tool.fn


@pytest.fixture()
def mcp():
    m = FastMCP("test")
    register_workload_tools(m)
    return m


def _stub_context(monkeypatch, client, default_slug="unity"):
    """Patch get_plane_client_context as imported into plane_mcp.tools.workload.

    Mirrors the real resolution order's per-call override: a non-empty
    workspace_slug argument wins over the default.
    """

    def _fake(workspace_slug=None):
        return PlaneClientContext(client=client, workspace_slug=workspace_slug or default_slug)

    monkeypatch.setattr("plane_mcp.tools.workload.get_plane_client_context", _fake)


def _mock_response(status_code: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=payload if payload is not None else {}, request=_REQUEST)


class TestGetWorkSettings:
    def test_returns_default_fallback_payload(self, monkeypatch, mcp):
        """A workspace with no settings row yet still gets a value — the
        server-side default (8h/day, Mon-Fri, week starts Monday)."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured.update(method=method, url=url)
            return _mock_response(
                200,
                {"max_daily_hours": 8.0, "workdays": [1, 2, 3, 4, 5], "week_start_day": 1},
            )

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "get_work_settings")
        result = fn()

        assert captured["method"] == "GET"
        assert captured["url"] == "https://plane.the1studio.org/api/v1/workspaces/unity/work-settings/"
        assert result == {"max_daily_hours": 8.0, "workdays": [1, 2, 3, 4, 5], "week_start_day": 1}

    def test_honours_per_call_workspace_slug(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client, default_slug="unity")

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured["url"] = url
            return _mock_response(200, {"max_daily_hours": 8.0, "workdays": [1, 2, 3, 4, 5], "week_start_day": 1})

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "get_work_settings")
        fn(workspace_slug="cocos")

        assert "/workspaces/cocos/work-settings/" in captured["url"]

    def test_403_for_non_member_is_not_masked(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(403, {"error": "You do not have permission to perform this action."})

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "get_work_settings")
        with pytest.raises(httpx.HTTPStatusError):
            fn()


class TestSetWorkSettings:
    def test_put_round_trip_sends_exact_body(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured.update(method=method, url=url, json=json)
            return _mock_response(
                200,
                {"max_daily_hours": 6.5, "workdays": [1, 2, 3, 4, 5, 6], "week_start_day": 0},
            )

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "set_work_settings")
        result = fn(max_daily_hours=6.5, workdays=[6, 1, 2, 3, 4, 5], week_start_day=0)

        assert captured["method"] == "PUT"
        assert captured["url"] == "https://plane.the1studio.org/api/v1/workspaces/unity/work-settings/"
        # The tool sends the caller's own workdays order through unmodified —
        # normalization to ascending order is the server's job (serializer's
        # validate_workdays), not this tool's.
        assert captured["json"] == {
            "max_daily_hours": 6.5,
            "workdays": [6, 1, 2, 3, 4, 5],
            "week_start_day": 0,
        }
        assert result == {"max_daily_hours": 6.5, "workdays": [1, 2, 3, 4, 5, 6], "week_start_day": 0}

    def test_empty_workdays_400_is_not_masked(self, monkeypatch, mcp):
        """The server rejects empty workdays with a 400 (divide-by-zero
        guard) — this tool must surface that, not swallow it or default it."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(400, {"error": "workdays must not be empty"})

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "set_work_settings")
        with pytest.raises(httpx.HTTPStatusError, match="workdays must not be empty"):
            fn(max_daily_hours=8.0, workdays=[], week_start_day=1)

    def test_put_as_member_is_403_not_masked(self, monkeypatch, mcp):
        """PUT is ADMIN-only server-side; a MEMBER's attempt must surface as
        a real 403, never silently succeed or get retried as GET."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(403, {"error": "You do not have permission to perform this action."})

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "set_work_settings")
        with pytest.raises(httpx.HTTPStatusError):
            fn(max_daily_hours=8.0, workdays=[1, 2, 3, 4, 5], week_start_day=1)
