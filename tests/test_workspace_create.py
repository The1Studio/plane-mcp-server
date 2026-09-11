"""Tests for `create_workspace` (The1Studio/plane-mcp-server#43).

Targets the instance-level `POST /api/v1/workspaces/`, an endpoint the
The1Studio Plane fork ships separately (`The1Studio/plane#107`). It is the one
workspace tool that is NOT slug-scoped, so the tests here pin that too: the
call must resolve its client with `require_workspace=False`, since at the moment
it runs there is no workspace to name.

Every HTTP call is mocked with real `httpx.Response` objects; nothing here
touches a network or creates anything.
"""

import asyncio

import httpx
import pytest
from fastmcp import FastMCP

from plane_mcp.client import PlaneClientContext
from plane_mcp.tools.workspaces import register_workspace_tools

_REQUEST = httpx.Request("POST", "https://plane.the1studio.org/api/v1/")

_CREATED = {
    "id": "ws-uuid-1",
    "name": "DevOps",
    "slug": "devops",
    "owner": "user-uuid-1",
    "organization_size": "1-10",
    "logo_url": None,
    "created_at": "2026-09-11T00:00:00Z",
    "updated_at": "2026-09-11T00:00:00Z",
    "role": 20,
    "total_members": 1,
}


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
    tool = asyncio.run(mcp.get_tool(name))
    return tool.fn


@pytest.fixture()
def mcp():
    m = FastMCP("test")
    register_workspace_tools(m)
    return m


def _stub_context(monkeypatch, client, recorded: dict | None = None):
    """Patch the context resolver, recording whether a workspace was required.

    `create_workspace` must pass `require_workspace=False` -- on a server whose
    only configured slug is the workspace about to be created, requiring one
    would raise `MissingWorkspaceError` before the request ever went out.
    """

    def _fake(workspace_slug=None, require_workspace=True):
        if recorded is not None:
            recorded["workspace_slug"] = workspace_slug
            recorded["require_workspace"] = require_workspace
        return PlaneClientContext(client=client, workspace_slug=workspace_slug or "unity")

    monkeypatch.setattr("plane_mcp.tools.workspaces.get_plane_client_context", _fake)


def _capture(monkeypatch, response: httpx.Response):
    calls: list[dict] = []

    def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
        calls.append({"method": method, "url": url, "json": json})
        return response

    monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)
    return calls


def _mock_response(status_code: int, payload=None) -> httpx.Response:
    return httpx.Response(status_code, json=payload if payload is not None else {}, request=_REQUEST)


class TestCreateWorkspace:
    def test_posts_to_instance_collection_path(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(monkeypatch, _mock_response(201, _CREATED))

        fn = _get_tool_fn(mcp, "create_workspace")
        fn(name="DevOps", slug="devops", organization_size="1-10")

        assert calls[0]["method"] == "POST"
        assert calls[0]["url"].endswith("/api/v1/workspaces/")
        assert calls[0]["json"] == {"name": "DevOps", "slug": "devops", "organization_size": "1-10"}

    def test_does_not_require_a_workspace_slug(self, monkeypatch, mcp):
        """There is no slug yet -- resolution must run with require_workspace=False."""
        client = _FakeClient()
        recorded: dict = {}
        _stub_context(monkeypatch, client, recorded)
        _capture(monkeypatch, _mock_response(201, _CREATED))

        _get_tool_fn(mcp, "create_workspace")(name="DevOps", slug="devops")

        assert recorded["require_workspace"] is False
        assert recorded["workspace_slug"] is None

    def test_returns_created_fields_with_next_steps(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, _mock_response(201, _CREATED))

        result = _get_tool_fn(mcp, "create_workspace")(name="DevOps", slug="devops")

        assert result["id"] == "ws-uuid-1"
        assert result["slug"] == "devops"
        assert result["role"] == 20
        assert result["total_members"] == 1

    def test_next_steps_name_set_workspace_and_the_env_var(self, monkeypatch, mcp):
        """Both follow-ups are easy to miss, so both must be in the result.

        Without the PLANE_WORKSPACE_SLUGS note the next session cannot address
        the new workspace at all -- the same trap as the discovery issues.
        """
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, _mock_response(201, _CREATED))

        result = _get_tool_fn(mcp, "create_workspace")(name="DevOps", slug="devops")

        steps = " ".join(result["next_steps"])
        assert "set_workspace" in steps
        assert "PLANE_WORKSPACE_SLUGS" in steps

    def test_next_steps_use_the_server_returned_slug(self, monkeypatch, mcp):
        """A server that normalizes the slug must be quoted correctly."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, _mock_response(201, {**_CREATED, "slug": "dev-ops"}))

        result = _get_tool_fn(mcp, "create_workspace")(name="DevOps", slug="devops")
        assert "dev-ops" in " ".join(result["next_steps"])

    def test_omits_organization_size_when_not_given(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(monkeypatch, _mock_response(201, _CREATED))

        _get_tool_fn(mcp, "create_workspace")(name="DevOps", slug="devops")
        assert calls[0]["json"]["organization_size"] is None

    def test_trims_name_and_slug(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(monkeypatch, _mock_response(201, _CREATED))

        _get_tool_fn(mcp, "create_workspace")(name="  DevOps  ", slug="  devops  ")
        assert calls[0]["json"]["name"] == "DevOps"
        assert calls[0]["json"]["slug"] == "devops"

    def test_rejects_empty_name(self, mcp):
        fn = _get_tool_fn(mcp, "create_workspace")
        with pytest.raises(ValueError, match="non-empty name"):
            fn(name="   ", slug="devops")

    def test_rejects_empty_slug(self, mcp):
        fn = _get_tool_fn(mcp, "create_workspace")
        with pytest.raises(ValueError, match="non-empty slug"):
            fn(name="DevOps", slug="   ")

    def test_403_when_not_instance_admin(self, monkeypatch, mcp):
        """A non-instance-admin key must be told that, not shown a bare 403."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(
            monkeypatch,
            _mock_response(403, {"error": "Instance admin required", "error_code": "INSTANCE_ADMIN_REQUIRED"}),
        )

        fn = _get_tool_fn(mcp, "create_workspace")
        with pytest.raises(RuntimeError, match="instance admin"):
            fn(name="DevOps", slug="devops")

    def test_403_when_creation_disabled_is_distinct_from_admin(self, monkeypatch, mcp):
        """The two 403 causes share a status code and must not be conflated."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(
            monkeypatch,
            _mock_response(403, {"error": "Disabled", "error_code": "WORKSPACE_CREATION_DISABLED"}),
        )

        fn = _get_tool_fn(mcp, "create_workspace")
        with pytest.raises(RuntimeError, match="WORKSPACE_CREATION_DISABLED"):
            fn(name="DevOps", slug="devops")

    def test_409_surfaces_the_slug_conflict(self, monkeypatch, mcp):
        """A taken slug is the likeliest failure and must not read as a bare 409.

        The 409 body is `{"slug": ..., "error_code": ...}` with no `error` key,
        so the shared `_send` error extraction finds nothing to surface.
        """
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(
            monkeypatch,
            _mock_response(
                409,
                {"slug": "The workspace with the slug already exists", "error_code": "WORKSPACE_SLUG_EXISTS"},
            ),
        )

        fn = _get_tool_fn(mcp, "create_workspace")
        with pytest.raises(RuntimeError, match="already exists"):
            fn(name="DevOps", slug="devops")

    def test_400_surfaces_field_errors(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(
            monkeypatch,
            _mock_response(
                400, {"slug": ["Enter a valid 'slug' consisting of letters, numbers, underscores or hyphens."]}
            ),
        )

        fn = _get_tool_fn(mcp, "create_workspace")
        with pytest.raises(RuntimeError, match="rejected the workspace payload"):
            fn(name="DevOps", slug="dev ops")

    def test_404_is_not_masked_as_a_fork_app_problem(self, monkeypatch, mcp):
        """A missing endpoint reads as 'not found', never as the project_ext message.

        Routing this through `_fork_endpoint_error` would blame a missing
        The1Studio fork app; the endpoint is fork-owned but the failure mode
        the caller needs named is 'this server predates it'.
        """
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, _mock_response(404, {"error": "Not found."}))

        fn = _get_tool_fn(mcp, "create_workspace")
        with pytest.raises(RuntimeError, match="404"):
            fn(name="DevOps", slug="devops")

    def test_does_not_auto_set_the_active_workspace(self, monkeypatch, mcp):
        """The tool must not silently retarget the session -- issues #10/#11."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, _mock_response(201, _CREATED))

        called: list = []
        monkeypatch.setattr("plane_mcp.tools.workspaces.set_active_workspace", lambda slug: called.append(slug))

        _get_tool_fn(mcp, "create_workspace")(name="DevOps", slug="devops")
        assert called == []
