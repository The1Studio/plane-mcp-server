"""Tests for `update_module(cascade=...)` and `preview_module_cascade`
(The1Studio fork feature, plans/260828-module-cascade-terminal-status/,
depends on The1Studio/plane branch `feat/module-cascade-terminal-status`).

`update_module`'s cascade path validates `status` against `ModuleStatusEnum`
(coercing an unrecognized value to `None`, exactly as it did before `cascade`
existed) and, when the VALIDATED status is terminal ("completed" or
"cancelled"), POSTs to the fork's module cascade-apply endpoint via the shared
`_send` helper in `plane_mcp/tools/cascade_ext.py` instead of a plain PATCH.
All HTTP and SDK calls are mocked; nothing here calls a live server.

Deliberately exercises the FAILURE-shaped defaults: cascade=False (or a
non-terminal target, or a raw status string that fails validation) must be
byte-for-byte the existing plain PATCH, so every pre-existing caller of
`update_module` keeps working unchanged.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP

from plane_mcp.client import PlaneClientContext
from plane_mcp.tools.cascade_ext import register_cascade_ext_tools
from plane_mcp.tools.modules import register_module_tools

_REQUEST = httpx.Request("GET", "https://plane.the1studio.org/api/v1/")


class _FakeConfig:
    """Mimics the subset of plane-sdk's client.config that `_send` reads."""

    def __init__(self):
        self.base_path = "https://plane.the1studio.org/api/v1"
        self.api_key = "test-api-key"
        self.access_token = None


class _FakeModules:
    """Mimics `client.modules` — only `.update` is exercised here."""

    def __init__(self):
        self.update_calls: list[dict] = []

    def update(self, workspace_slug, project_id, module_id, data):
        self.update_calls.append(
            {
                "workspace_slug": workspace_slug,
                "project_id": project_id,
                "module_id": module_id,
                "data": data,
            }
        )
        return SimpleNamespace(id=module_id, status=data.status, name=data.name)


class _FakeClient:
    def __init__(self):
        self.config = _FakeConfig()
        self.modules = _FakeModules()


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
    register_module_tools(m)
    register_cascade_ext_tools(m)
    return m


def _stub_context(monkeypatch, client, default_slug="unity"):
    """Mirrors the real resolution order's per-call override: a non-empty
    workspace_slug argument wins over the default."""

    def _fake(workspace_slug=None):
        return PlaneClientContext(client=client, workspace_slug=workspace_slug or default_slug)

    monkeypatch.setattr("plane_mcp.tools.modules.get_plane_client_context", _fake)
    monkeypatch.setattr("plane_mcp.tools.cascade_ext.get_plane_client_context", _fake)


def _mock_response(status_code: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=payload if payload is not None else {}, request=_REQUEST)


def _fail_if_called(method, url, headers=None, json=None, params=None, timeout=None):
    """Installed when a test must prove zero cascade-ext HTTP calls fire."""
    raise AssertionError(f"unexpected cascade-ext HTTP call: {method} {url}")


class TestUpdateModuleCascadeDefaults:
    """cascade defaults False and must never surprise-fire — the gate for
    'additive, no existing caller's behavior changed'."""

    def test_cascade_false_is_plain_patch_even_for_a_terminal_status(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fail_if_called)

        fn = _get_tool_fn(mcp, "update_module")
        result = fn(project_id="proj-1", module_id="mod-1", status="completed")

        # cascade defaults False -> zero cascade-ext calls, plain PATCH only.
        assert len(client.modules.update_calls) == 1
        assert client.modules.update_calls[0]["data"].status == "completed"
        assert result.status == "completed"

    def test_cascade_true_but_status_not_terminal_is_plain_patch(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fail_if_called)

        fn = _get_tool_fn(mcp, "update_module")
        result = fn(project_id="proj-1", module_id="mod-1", status="in-progress", cascade=True)

        assert len(client.modules.update_calls) == 1
        assert client.modules.update_calls[0]["data"].status == "in-progress"
        assert result.status == "in-progress"

    def test_unrecognized_status_string_is_coerced_to_none_and_never_cascades(self, monkeypatch, mcp):
        """A caller passing a raw string that fails ModuleStatusEnum
        validation (e.g. wrong casing) must not fire the cascade branch,
        which keys off the VALIDATED status — never the raw argument."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fail_if_called)

        fn = _get_tool_fn(mcp, "update_module")
        result = fn(project_id="proj-1", module_id="mod-1", status="Completed", cascade=True)

        assert len(client.modules.update_calls) == 1
        assert client.modules.update_calls[0]["data"].status is None
        assert result.status is None

    def test_cascade_true_with_no_status_change_is_plain_patch(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fail_if_called)

        fn = _get_tool_fn(mcp, "update_module")
        fn(project_id="proj-1", module_id="mod-1", name="Renamed", cascade=True)

        assert len(client.modules.update_calls) == 1
        assert client.modules.update_calls[0]["data"].name == "Renamed"


class TestUpdateModuleCascadeApply:
    """cascade=True + a terminal validated status calls cascade-apply."""

    def test_terminal_status_calls_cascade_apply_with_no_item_ids(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured.update(method=method, url=url, json=json)
            return _mock_response(
                200, {"module": "mod-1", "status": "completed", "updated": ["wi-2", "wi-3"], "rejected": []}
            )

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_module")
        result = fn(project_id="proj-1", module_id="mod-1", status="completed", cascade=True)

        assert captured["method"] == "POST"
        assert (
            captured["url"] == "https://plane.the1studio.org/api/cascade-ext/workspaces/unity/projects/proj-1"
            "/modules/mod-1/cascade-apply/"
        )
        assert captured["json"] == {"status": "completed"}
        assert result == {"module": "mod-1", "status": "completed", "updated": ["wi-2", "wi-3"], "rejected": []}

        # Only `status` was set in this call -> nothing left for a plain PATCH.
        assert client.modules.update_calls == []

    def test_cancelled_status_also_cascades(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(200, {"module": "mod-1", "status": "cancelled", "updated": [], "rejected": []})

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_module")
        result = fn(project_id="proj-1", module_id="mod-1", status="cancelled", cascade=True)

        assert result["module"] == "mod-1"
        assert result["status"] == "cancelled"

    def test_other_fields_set_alongside_cascade_still_apply_via_plain_patch(self, monkeypatch, mcp):
        """cascade-apply only ever touches `status` server-side — any other
        field set in the same call must not be silently dropped."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(200, {"module": "mod-1", "status": "completed", "updated": ["wi-2"], "rejected": []})

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_module")
        result = fn(
            project_id="proj-1",
            module_id="mod-1",
            status="completed",
            name="Q3 Launch (shipped)",
            cascade=True,
        )

        # The rename lands via the ordinary PATCH, with `status` excluded
        # from it (cascade-apply owns the status move).
        assert len(client.modules.update_calls) == 1
        applied = client.modules.update_calls[0]["data"]
        assert applied.name == "Q3 Launch (shipped)"
        assert applied.status is None
        # The tool's return value is still the cascade-apply response.
        assert result == {"module": "mod-1", "status": "completed", "updated": ["wi-2"], "rejected": []}

    def test_rejected_items_are_surfaced_not_swallowed(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(
                200,
                {
                    "module": "mod-1",
                    "status": "completed",
                    "updated": ["wi-2"],
                    "rejected": [{"id": "wi-3", "reason": "no_permission"}],
                },
            )

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_module")
        result = fn(project_id="proj-1", module_id="mod-1", status="completed", cascade=True)

        assert result["rejected"] == [{"id": "wi-3", "reason": "no_permission"}]

    def test_over_cap_400_is_a_readable_refusal_not_retried_as_transport_failure(self, monkeypatch, mcp):
        """M4: exceeding MAX_MODULE_CASCADE_ITEMS (100) is a 400 refusal
        having written nothing (module status included) — not a partial
        apply and not a bare transport error a headless caller might retry."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(
                400, {"error": "module exceeds cascade cap of 100 live items", "total_live": 240, "cap": 100}
            )

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_module")
        with pytest.raises(httpx.HTTPStatusError, match="exceeds cascade cap of 100"):
            fn(project_id="proj-1", module_id="mod-1", status="completed", cascade=True)

        # The refusal must not silently fall back to a plain PATCH either.
        assert client.modules.update_calls == []

    def test_404_raises_actionable_error_not_a_silent_no_op(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(404, {"error": "module not found"})

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_module")
        with pytest.raises(httpx.HTTPStatusError, match="module not found"):
            fn(project_id="proj-1", module_id="mod-1", status="completed", cascade=True)


class TestPreviewModuleCascade:
    def test_sends_status_as_query_param_hits_cascade_ext_not_v1(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured.update(method=method, url=url, params=params)
            return _mock_response(
                200,
                {
                    "target_group": "completed",
                    "depth_capped": False,
                    "over_cap": False,
                    "cap": 100,
                    "summary": {"total_live": 0, "eligible": 0, "ineligible": 0, "already_terminal": 0},
                    "items": [],
                },
            )

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "preview_module_cascade")
        result = fn(workspace_slug="unity", project_id="proj-1", module_id="mod-1", status="completed")

        assert captured["method"] == "GET"
        assert (
            captured["url"] == "https://plane.the1studio.org/api/cascade-ext/workspaces/unity/projects/proj-1"
            "/modules/mod-1/cascade-preview/"
        )
        # Module preview takes `status` — NOT `group`, which the work-item
        # preview uses. The two are deliberately not unified server-side.
        assert captured["params"] == {"status": "completed"}
        assert "/api/v1/" not in captured["url"]
        assert result["items"] == []

    def test_over_cap_response_is_passed_through_with_empty_items(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(
                200,
                {
                    "target_group": "completed",
                    "depth_capped": False,
                    "over_cap": True,
                    "cap": 100,
                    "summary": {"total_live": 240, "eligible": 240, "ineligible": 0, "already_terminal": 0},
                    "items": [],
                },
            )

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "preview_module_cascade")
        result = fn(workspace_slug="unity", project_id="proj-1", module_id="mod-1", status="completed")

        assert result["over_cap"] is True
        assert result["items"] == []
        assert result["summary"]["total_live"] == 240

    def test_400_bad_status_is_not_masked(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(400, {"error": "status must be one of completed|cancelled"})

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "preview_module_cascade")
        with pytest.raises(httpx.HTTPStatusError, match="status must be one of"):
            fn(workspace_slug="unity", project_id="proj-1", module_id="mod-1", status="not-a-status")
