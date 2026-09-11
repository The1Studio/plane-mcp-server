"""Tests for the workspace-invitation tools (The1Studio/plane-mcp-server#42).

`invite_workspace_member`, `list_workspace_invites` and `revoke_workspace_invite`
wrap Plane's existing upstream `WorkspaceInvitationsViewset`
(`/workspaces/{slug}/invitations/`). The plane-sdk models no invitation resource
at all, so these go through the shared `_send` helper -- hence every HTTP call
is mocked here with real `httpx.Response` objects and nothing touches a network.

Deliberately exercises the FAILURE states the issue called out:

* a 403 must explain the owner-only permission (`WorkspaceOwnerPermission`,
  unlike the workspace-admin check every neighbouring tool uses);
* a 404 must NOT be masked into the "you need the The1Studio fork's project_ext
  app" message -- this endpoint ships upstream, so that message would be a lie.
"""

import asyncio

import httpx
import pytest
from fastmcp import FastMCP

from plane_mcp.client import PlaneClientContext
from plane_mcp.tools.workspaces import register_workspace_tools

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
    register_workspace_tools(m)
    return m


def _stub_context(monkeypatch, client, default_slug="unity"):
    """Patch get_plane_client_context as imported into plane_mcp.tools.workspaces.

    Mirrors the real resolution order's per-call override: a non-empty
    workspace_slug argument wins over the default.
    """

    def _fake(workspace_slug=None, require_workspace=True):
        return PlaneClientContext(client=client, workspace_slug=workspace_slug or default_slug)

    monkeypatch.setattr("plane_mcp.tools.workspaces.get_plane_client_context", _fake)


def _mock_response(status_code: int, payload=None) -> httpx.Response:
    return httpx.Response(status_code, json=payload if payload is not None else {}, request=_REQUEST)


def _capture(monkeypatch, responses):
    """Install a fake httpx.request that records the call and returns `responses` in order.

    A list lets one test drive a multi-request flow (list-then-create) through
    the same tool call.
    """
    calls: list[dict] = []
    queue = list(responses)

    def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
        calls.append({"method": method, "url": url, "json": json, "headers": headers})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)
    return calls


_PENDING_INVITE = {
    "id": "invite-1",
    "email": "hieuld@the1studio.org",
    "role": 15,
    "created_at": "2026-09-11T00:00:00Z",
    "updated_at": "2026-09-11T00:00:00Z",
    "responded_at": None,
    "accepted": False,
}


class TestListWorkspaceInvites:
    def test_returns_invites_from_workspace_scoped_path(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(monkeypatch, [_mock_response(200, [_PENDING_INVITE])])

        fn = _get_tool_fn(mcp, "list_workspace_invites")
        result = fn(workspace_slug="devops")

        assert result == [_PENDING_INVITE]
        assert calls[0]["method"] == "GET"
        assert calls[0]["url"].endswith("/workspaces/devops/invitations/")

    def test_defaults_to_session_slug_when_none_given(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client, default_slug="cocos")
        calls = _capture(monkeypatch, [_mock_response(200, [])])

        fn = _get_tool_fn(mcp, "list_workspace_invites")
        assert fn() == []
        assert calls[0]["url"].endswith("/workspaces/cocos/invitations/")

    def test_403_names_the_owner_only_permission(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, [_mock_response(403, {"error": "Permission denied"})])

        fn = _get_tool_fn(mcp, "list_workspace_invites")
        with pytest.raises(RuntimeError, match="OWNER"):
            fn()

    def test_404_is_not_masked_as_a_fork_app_problem(self, monkeypatch, mcp):
        """The invitation route ships upstream -- a 404 must stay a 404.

        Reinterpreting it as "install the The1Studio fork" (the `project_ext`
        message) would send the caller chasing a nonexistent deployment problem.
        """
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, [_mock_response(404, {"error": "Not found."})])

        fn = _get_tool_fn(mcp, "list_workspace_invites")
        with pytest.raises(httpx.HTTPStatusError):
            fn()


class TestInviteWorkspaceMember:
    def test_posts_email_and_role(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(
            monkeypatch,
            [_mock_response(200, []), _mock_response(201, _PENDING_INVITE)],
        )

        fn = _get_tool_fn(mcp, "invite_workspace_member")
        result = fn(email="hieuld@the1studio.org", role=20, workspace_slug="devops")

        # First call lists (dedupe check), second creates.
        assert calls[0]["method"] == "GET"
        assert calls[1]["method"] == "POST"
        assert calls[1]["url"].endswith("/workspaces/devops/invitations/")
        assert calls[1]["json"] == {"email": "hieuld@the1studio.org", "role": 20}
        assert result["id"] == "invite-1"
        assert "already_invited" not in result

    def test_default_role_is_member(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(monkeypatch, [_mock_response(200, []), _mock_response(201, _PENDING_INVITE)])

        _get_tool_fn(mcp, "invite_workspace_member")(email="a@b.com")
        assert calls[1]["json"]["role"] == 15

    def test_returns_already_invited_instead_of_duplicating(self, monkeypatch, mcp):
        """A repeat invite for the same address must not send a second email."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(monkeypatch, [_mock_response(200, [_PENDING_INVITE])])

        fn = _get_tool_fn(mcp, "invite_workspace_member")
        result = fn(email="hieuld@the1studio.org")

        assert result["already_invited"] is True
        assert result["id"] == "invite-1"
        assert result["email"] == "hieuld@the1studio.org"
        assert "note" in result
        # Only the list ran -- no POST, so no email was sent.
        assert [c["method"] for c in calls] == ["GET"]

    def test_dedupe_ignores_case_and_whitespace(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(monkeypatch, [_mock_response(200, [_PENDING_INVITE])])

        fn = _get_tool_fn(mcp, "invite_workspace_member")
        result = fn(email="  Hieuld@The1Studio.org  ")

        assert result["already_invited"] is True
        assert [c["method"] for c in calls] == ["GET"]

    def test_different_email_still_creates(self, monkeypatch, mcp):
        """The dedupe must not swallow a genuinely new invite."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(monkeypatch, [_mock_response(200, [_PENDING_INVITE]), _mock_response(201, _PENDING_INVITE)])

        _get_tool_fn(mcp, "invite_workspace_member")(email="someone@else.com")
        assert calls[1]["method"] == "POST"

    def test_accepts_paginated_list_envelope(self, monkeypatch, mcp):
        """A `{"results": [...]}` envelope must still dedupe, not crash."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(monkeypatch, [_mock_response(200, {"results": [_PENDING_INVITE]})])

        result = _get_tool_fn(mcp, "invite_workspace_member")(email="hieuld@the1studio.org")
        assert result["already_invited"] is True
        assert [c["method"] for c in calls] == ["GET"]

    def test_rejects_empty_email(self, mcp):
        fn = _get_tool_fn(mcp, "invite_workspace_member")
        with pytest.raises(ValueError, match="non-empty email"):
            fn(email="   ")

    def test_403_names_the_owner_only_permission(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, [_mock_response(403, {"error": "Permission denied"})])

        fn = _get_tool_fn(mcp, "invite_workspace_member")
        with pytest.raises(RuntimeError, match="WorkspaceOwnerPermission"):
            fn(email="new@the1studio.org")

    def test_403_does_not_claim_to_disambiguate_the_slug(self, monkeypatch, mcp):
        """Plane returns the same 403 for an unreachable slug -- say so."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, [_mock_response(403, {"error": "Permission denied"})])

        fn = _get_tool_fn(mcp, "invite_workspace_member")
        with pytest.raises(RuntimeError, match="cannot reach at all"):
            fn(email="new@the1studio.org")

    def test_403_on_create_after_empty_list_is_also_explained(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, [_mock_response(200, []), _mock_response(403, {"error": "Permission denied"})])

        fn = _get_tool_fn(mcp, "invite_workspace_member")
        with pytest.raises(RuntimeError, match="OWNER"):
            fn(email="new@the1studio.org")


class TestRevokeWorkspaceInvite:
    def test_deletes_and_reports_revoked(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        calls = _capture(monkeypatch, [_mock_response(204)])

        fn = _get_tool_fn(mcp, "revoke_workspace_invite")
        result = fn(invite_id="invite-1", workspace_slug="devops")

        assert result == {"revoked": True, "invite_id": "invite-1"}
        assert calls[0]["method"] == "DELETE"
        assert calls[0]["url"].endswith("/workspaces/devops/invitations/invite-1/")

    def test_rejects_empty_invite_id(self, mcp):
        fn = _get_tool_fn(mcp, "revoke_workspace_invite")
        with pytest.raises(ValueError, match="non-empty invite_id"):
            fn(invite_id="  ")

    def test_already_accepted_400_surfaces_with_the_server_reason(self, monkeypatch, mcp):
        """The server refuses to revoke an accepted invite; the caller must see why."""
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, [_mock_response(400, {"error": "Invite already accepted"})])

        fn = _get_tool_fn(mcp, "revoke_workspace_invite")
        with pytest.raises(httpx.HTTPStatusError, match="Invite already accepted"):
            fn(invite_id="invite-1")

    def test_403_names_the_owner_only_permission(self, monkeypatch, mcp):
        client = _FakeClient()
        _stub_context(monkeypatch, client)
        _capture(monkeypatch, [_mock_response(403, {"error": "Permission denied"})])

        fn = _get_tool_fn(mcp, "revoke_workspace_invite")
        with pytest.raises(RuntimeError, match="OWNER"):
            fn(invite_id="invite-1")
