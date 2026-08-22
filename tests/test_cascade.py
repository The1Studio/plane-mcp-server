"""Tests for `update_work_item(cascade=...)` and `preview_work_item_cascade`
(The1Studio fork feature, depends on The1Studio/plane#54's `cascade_ext` app).

`update_work_item`'s cascade path calls `client.states.retrieve` to read the
target state's `group` (NEVER its name — states are renameable per project)
and, when that group is terminal, POSTs to the fork's cascade-apply endpoint
via the shared `_send` helper in `plane_mcp/tools/cascade_ext.py` instead of
a plain PATCH. All HTTP and SDK calls are mocked; nothing here calls a live
server.

Deliberately exercises the FAILURE-shaped defaults: cascade=False (or no
state change, or a non-terminal target) must be byte-for-byte the existing
plain PATCH, so every pre-existing caller of `update_work_item` keeps working
unchanged.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP

from plane_mcp.client import PlaneClientContext
from plane_mcp.tools.cascade_ext import register_cascade_ext_tools
from plane_mcp.tools.work_items import register_work_item_tools

_REQUEST = httpx.Request("GET", "https://plane.the1studio.org/api/v1/")


class _FakeConfig:
    """Mimics the subset of plane-sdk's client.config that `_send` reads."""

    def __init__(self):
        self.base_path = "https://plane.the1studio.org/api/v1"
        self.api_key = "test-api-key"
        self.access_token = None


class _FakeStates:
    """Mimics `client.states` — only `.retrieve` is exercised here."""

    def __init__(self, group_by_id: dict[str, str]):
        self._group_by_id = group_by_id
        self.calls: list[dict] = []

    def retrieve(self, workspace_slug, project_id, state_id):
        self.calls.append(
            {"workspace_slug": workspace_slug, "project_id": project_id, "state_id": state_id}
        )
        # `name` is deliberately misleading in some tests — terminality must
        # be read from `group`, never from this.
        return SimpleNamespace(id=state_id, group=self._group_by_id[state_id], name="Custom Name")


class _FakeWorkItems:
    """Mimics `client.work_items` — only `.update` is exercised here."""

    def __init__(self):
        self.update_calls: list[dict] = []

    def update(self, workspace_slug, project_id, work_item_id, data):
        self.update_calls.append(
            {
                "workspace_slug": workspace_slug,
                "project_id": project_id,
                "work_item_id": work_item_id,
                "data": data,
            }
        )
        return SimpleNamespace(id=work_item_id, state=data.state, name=data.name)


class _FakeClient:
    def __init__(self, group_by_id: dict[str, str] | None = None):
        self.config = _FakeConfig()
        self.states = _FakeStates(group_by_id or {})
        self.work_items = _FakeWorkItems()


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
    register_work_item_tools(m)
    register_cascade_ext_tools(m)
    return m


def _stub_context(monkeypatch, client, default_slug="unity"):
    """Mirrors the real resolution order's per-call override: a non-empty
    workspace_slug argument wins over the default."""

    def _fake(workspace_slug=None):
        return PlaneClientContext(client=client, workspace_slug=workspace_slug or default_slug)

    monkeypatch.setattr("plane_mcp.tools.work_items.get_plane_client_context", _fake)
    monkeypatch.setattr("plane_mcp.tools.cascade_ext.get_plane_client_context", _fake)


def _mock_response(status_code: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=payload if payload is not None else {}, request=_REQUEST)


class TestUpdateWorkItemCascadeDefaults:
    """cascade defaults False and must never surprise-fire."""

    def test_cascade_false_is_plain_patch_even_for_a_terminal_state(self, monkeypatch, mcp):
        client = _FakeClient(group_by_id={"state-done": "completed"})
        _stub_context(monkeypatch, client)

        fn = _get_tool_fn(mcp, "update_work_item")
        result = fn(project_id="proj-1", work_item_id="wi-1", state="state-done")

        # cascade defaults False -> never touches states.retrieve, plain PATCH.
        assert client.states.calls == []
        assert len(client.work_items.update_calls) == 1
        assert client.work_items.update_calls[0]["data"].state == "state-done"
        assert result.state == "state-done"

    def test_cascade_true_with_no_state_change_is_plain_patch(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        fn = _get_tool_fn(mcp, "update_work_item")
        fn(project_id="proj-1", work_item_id="wi-1", name="Renamed", cascade=True)

        # No `state` in this call -> nothing to cascade, and no state lookup.
        assert client.states.calls == []
        assert len(client.work_items.update_calls) == 1
        assert client.work_items.update_calls[0]["data"].name == "Renamed"

    def test_cascade_true_but_target_state_not_terminal_is_plain_patch(self, monkeypatch, mcp):
        client = _FakeClient(group_by_id={"state-doing": "started"})
        _stub_context(monkeypatch, client)

        fn = _get_tool_fn(mcp, "update_work_item")
        result = fn(project_id="proj-1", work_item_id="wi-1", state="state-doing", cascade=True)

        assert len(client.states.calls) == 1
        assert len(client.work_items.update_calls) == 1
        assert client.work_items.update_calls[0]["data"].state == "state-doing"
        assert result.state == "state-doing"

    def test_terminality_is_read_from_group_never_from_name(self, monkeypatch, mcp):
        """The fake state's `name` is "Custom Name" for every state in this
        suite — a name-based check would be indistinguishable across cases.
        Only `group` may decide terminality."""
        client = _FakeClient(group_by_id={"state-x": "backlog"})
        _stub_context(monkeypatch, client)

        fn = _get_tool_fn(mcp, "update_work_item")
        fn(project_id="proj-1", work_item_id="wi-1", state="state-x", cascade=True)

        # backlog is not terminal -> plain PATCH, no cascade-apply HTTP call
        # was even attempted (verified indirectly: no httpx patch installed
        # in this test, so a call would raise/hang if attempted).
        assert len(client.work_items.update_calls) == 1
        assert client.work_items.update_calls[0]["data"].state == "state-x"


class TestUpdateWorkItemCascadeApply:
    """cascade=True + a terminal target state calls cascade-apply."""

    def test_terminal_state_calls_cascade_apply_with_no_child_ids(self, monkeypatch, mcp):
        client = _FakeClient(group_by_id={"state-done": "completed"})
        _stub_context(monkeypatch, client)

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured.update(method=method, url=url, json=json)
            return _mock_response(
                200, {"parent": "wi-1", "updated": ["wi-2", "wi-3"], "rejected": []}
            )

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_work_item")
        result = fn(project_id="proj-1", work_item_id="wi-1", state="state-done", cascade=True)

        assert captured["method"] == "POST"
        assert (
            captured["url"]
            == "https://plane.the1studio.org/api/cascade-ext/workspaces/unity/projects/proj-1"
            "/issues/wi-1/cascade-apply/"
        )
        assert captured["json"] == {"state_id": "state-done", "child_ids": None}
        assert result == {"parent": "wi-1", "updated": ["wi-2", "wi-3"], "rejected": []}

        # Only `state` was set in this call -> nothing left for a plain PATCH.
        assert client.work_items.update_calls == []

    def test_cancelled_group_also_cascades(self, monkeypatch, mcp):
        client = _FakeClient(group_by_id={"state-wontfix": "cancelled"})
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(200, {"parent": "wi-1", "updated": [], "rejected": []})

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_work_item")
        result = fn(project_id="proj-1", work_item_id="wi-1", state="state-wontfix", cascade=True)

        assert result["parent"] == "wi-1"

    def test_other_fields_set_alongside_cascade_still_apply_via_plain_patch(self, monkeypatch, mcp):
        """cascade-apply only ever touches `state` server-side — any other
        field set in the same call must not be silently dropped."""
        client = _FakeClient(group_by_id={"state-done": "completed"})
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(200, {"parent": "wi-1", "updated": ["wi-2"], "rejected": []})

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_work_item")
        result = fn(
            project_id="proj-1",
            work_item_id="wi-1",
            state="state-done",
            name="Shipped",
            cascade=True,
        )

        # The rename lands via the ordinary PATCH, with `state` excluded from
        # it (cascade-apply owns the state move).
        assert len(client.work_items.update_calls) == 1
        applied = client.work_items.update_calls[0]["data"]
        assert applied.name == "Shipped"
        assert applied.state is None
        # The tool's return value is still the cascade-apply response.
        assert result == {"parent": "wi-1", "updated": ["wi-2"], "rejected": []}

    def test_rejected_descendants_are_surfaced_not_swallowed(self, monkeypatch, mcp):
        client = _FakeClient(group_by_id={"state-done": "completed"})
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(
                200,
                {
                    "parent": "wi-1",
                    "updated": ["wi-2"],
                    "rejected": [{"id": "wi-3", "reason": "already terminal"}],
                },
            )

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_work_item")
        result = fn(project_id="proj-1", work_item_id="wi-1", state="state-done", cascade=True)

        assert result["rejected"] == [{"id": "wi-3", "reason": "already terminal"}]

    def test_404_raises_actionable_error_not_a_silent_no_op(self, monkeypatch, mcp):
        client = _FakeClient(group_by_id={"state-done": "completed"})
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(404, {"error": "issue not found"})

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "update_work_item")
        with pytest.raises(httpx.HTTPStatusError, match="issue not found"):
            fn(project_id="proj-1", work_item_id="wi-1", state="state-done", cascade=True)


class TestPreviewWorkItemCascade:
    def test_sends_group_as_query_param(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured.update(method=method, url=url, params=params)
            return _mock_response(
                200, {"target_group": "completed", "depth_capped": False, "descendants": []}
            )

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "preview_work_item_cascade")
        result = fn(project_id="proj-1", work_item_id="wi-1", group="completed")

        assert captured["method"] == "GET"
        assert (
            captured["url"]
            == "https://plane.the1studio.org/api/cascade-ext/workspaces/unity/projects/proj-1"
            "/issues/wi-1/cascade-preview/"
        )
        assert captured["params"] == {"group": "completed"}
        assert result["descendants"] == []

    def test_400_bad_group_is_not_masked(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(400, {"error": "group must be one of completed|cancelled"})

        monkeypatch.setattr("plane_mcp.tools.cascade_ext.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "preview_work_item_cascade")
        with pytest.raises(httpx.HTTPStatusError, match="group must be one of"):
            fn(project_id="proj-1", work_item_id="wi-1", group="not-a-group")
