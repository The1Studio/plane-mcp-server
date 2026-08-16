"""Tests for `list_all_projects` and `add_project_members` (The1Studio fork feature).

Both call the fork's `project_ext` endpoints via the shared `_send` helper (the
plane-sdk has no methods for either — see `plane_mcp/tools/project_visibility.py`
for the same pattern). All HTTP is mocked with real `httpx.Response` objects;
nothing here calls a live server.

Deliberately exercises the FAILURE state: a 404 (server predates the fork
endpoints, or is upstream Plane) must surface as an actionable `RuntimeError`,
never a raw `httpx.HTTPStatusError` traceback and never a silently-empty
result — an empty list would misread as "this workspace has no projects",
which is the exact misreading `list_all_projects` exists to prevent.
"""

import asyncio

import httpx
import pytest
from fastmcp import FastMCP

from plane_mcp.client import PlaneClientContext
from plane_mcp.tools.projects import register_project_tools

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
    register_project_tools(m)
    return m


def _stub_context(monkeypatch, client, default_slug="unity"):
    """Patch get_plane_client_context as imported into plane_mcp.tools.projects.

    Mirrors the real resolution order's per-call override: a non-empty
    workspace_slug argument wins over the default.
    """

    def _fake(workspace_slug=None):
        return PlaneClientContext(client=client, workspace_slug=workspace_slug or default_slug)

    monkeypatch.setattr("plane_mcp.tools.projects.get_plane_client_context", _fake)


def _mock_response(status_code: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=payload if payload is not None else {}, request=_REQUEST)


class TestListAllProjects:
    def test_returns_full_payload(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured.update(method=method, url=url)
            return _mock_response(
                200,
                {
                    "workspace_slug": "unity",
                    "count": 1,
                    "results": [
                        {
                            "id": "proj-1",
                            "name": "Secret Project",
                            "identifier": "SEC",
                            "network": 0,
                            "visibility": "private",
                            "is_member": False,
                        }
                    ],
                },
            )

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "list_all_projects")
        result = fn()

        assert captured["method"] == "GET"
        assert captured["url"] == "https://plane.the1studio.org/api/v1/workspaces/unity/all-projects/"
        assert result["count"] == 1
        assert result["results"][0]["is_member"] is False

    def test_honours_per_call_workspace_slug(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client, default_slug="unity")

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured["url"] = url
            return _mock_response(200, {"workspace_slug": "cocos", "count": 0, "results": []})

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "list_all_projects")
        fn(workspace_slug="cocos")

        assert "/workspaces/cocos/all-projects/" in captured["url"]

    def test_404_raises_actionable_error_not_empty_list(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(404, {"error": "Not found."})

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "list_all_projects")
        with pytest.raises(RuntimeError, match="project_ext"):
            fn()

    def test_non_404_error_is_not_masked(self, monkeypatch, mcp):
        """A 403 (not a workspace admin) must propagate as-is, not be
        reinterpreted as the fork-unavailable message."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(403, {"error": "Workspace admin required."})

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "list_all_projects")
        with pytest.raises(httpx.HTTPStatusError):
            fn()


class TestAddProjectMembers:
    def test_requires_at_least_one_project_id(self, mcp):
        fn = _get_tool_fn(mcp, "add_project_members")
        with pytest.raises(ValueError, match="at least one project_id"):
            fn(project_ids=[], user_id="user-1")

    def test_requires_user_id_or_email(self, mcp):
        fn = _get_tool_fn(mcp, "add_project_members")
        with pytest.raises(ValueError, match="user_id or email"):
            fn(project_ids=["proj-1"])

    def test_sends_user_id_body_to_bulk_path(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured.update(method=method, url=url, json=json)
            return _mock_response(
                200,
                {
                    "user_id": "user-1",
                    "email": None,
                    "role": 15,
                    "results": [
                        {"project_id": "proj-1", "created": True},
                        {"project_id": "proj-2", "created": False},
                    ],
                },
            )

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "add_project_members")
        result = fn(project_ids=["proj-1", "proj-2"], user_id="user-1")

        assert captured["method"] == "POST"
        assert captured["url"] == "https://plane.the1studio.org/api/v1/workspaces/unity/project-members/"
        assert captured["json"] == {"project_ids": ["proj-1", "proj-2"], "role": 15, "user_id": "user-1"}
        assert result["results"][0] == {"project_id": "proj-1", "created": True}
        assert result["results"][1] == {"project_id": "proj-2", "created": False}

    def test_sends_email_body_and_custom_role(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        captured: dict = {}

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            captured["json"] = json
            return _mock_response(
                200,
                {
                    "user_id": None,
                    "email": "a@b.com",
                    "role": 20,
                    "results": [{"project_id": "proj-1", "created": True}],
                },
            )

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "add_project_members")
        fn(project_ids=["proj-1"], email="a@b.com", role=20)

        assert captured["json"] == {"project_ids": ["proj-1"], "role": 20, "email": "a@b.com"}

    def test_idempotent_existing_member_reports_created_false(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(
                200,
                {
                    "user_id": "user-1",
                    "email": None,
                    "role": 15,
                    "results": [{"project_id": "proj-1", "created": False}],
                },
            )

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "add_project_members")
        result = fn(project_ids=["proj-1"], user_id="user-1")

        assert result["results"][0]["created"] is False

    def test_404_raises_actionable_error_when_a_project_id_is_unknown(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(404, {})

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "add_project_members")
        with pytest.raises(RuntimeError, match="project_ext"):
            fn(project_ids=["proj-1", "not-owned-by-workspace"], user_id="user-1")

    def test_400_bad_identity_is_not_masked(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)

        def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return _mock_response(400, {"error": "user not in workspace"})

        monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)

        fn = _get_tool_fn(mcp, "add_project_members")
        with pytest.raises(httpx.HTTPStatusError):
            fn(project_ids=["proj-1"], user_id="user-1")
