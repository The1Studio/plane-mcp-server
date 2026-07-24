"""Unit tests for the github_ext MCP tools (plane_mcp/tools/github_ext.py).

Unlike test_integration.py these need no live Plane server or credentials —
httpx.request and get_plane_client_context are mocked, matching the raw-http
pattern the tools use (see plane_mcp/tools/workload.py, which the github_ext
tools mirror). This is a new test file: no prior test coverage existed for
individual tool modules (only test_integration.py's end-to-end flow and the
oauth/aws_secrets/stateless_http infra tests).

pytest-asyncio is not a repo dependency, so — matching test_integration.py's
existing idiom — async client calls are driven via `asyncio.run()` inside
plain sync `test_*` functions rather than `@pytest.mark.asyncio`.
"""

import asyncio
import os

import pytest
from fastmcp import Client, FastMCP
from plane.models.work_items import PaginatedWorkItemLinkResponse, WorkItemLink

from plane_mcp.tools.github_ext import register_github_ext_tools

os.environ.setdefault("PLANE_API_KEY", "test-key")
os.environ.setdefault("PLANE_WORKSPACE_SLUG", "acme")
os.environ.setdefault("PLANE_BASE_URL", "https://plane.example.com")


class _FakeResponse:
    """Minimal stand-in for httpx.Response, success path only."""

    def __init__(self, payload):
        self.status_code = 200
        self.reason_phrase = "OK"
        self._payload = payload
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _build_mcp():
    mcp = FastMCP("test")
    register_github_ext_tools(mcp)
    return mcp


def _capture_requests(monkeypatch):
    """Patch httpx.request (as used inside plane_mcp.tools.github_ext) to
    record calls and return a canned {"rules": ...} payload.
    """
    calls = []

    def fake_request(method, url, headers=None, json=None, params=None, timeout=None):
        calls.append({"method": method, "url": url, "headers": headers, "json": json})
        return _FakeResponse({"rules": {"pr_opened": "In Progress"}})

    monkeypatch.setattr("plane_mcp.tools.github_ext.httpx.request", fake_request)
    return calls


def _call_tool(mcp, name, args):
    async def _run():
        async with Client(mcp) as client:
            return await client.call_tool(name, args)

    return asyncio.run(_run())


class TestGetGithubStateConfig:
    def test_default_targets_workspace_tier(self, monkeypatch):
        mcp = _build_mcp()
        calls = _capture_requests(monkeypatch)
        _call_tool(mcp, "get_github_state_config", {})
        assert calls[-1]["method"] == "GET"
        assert calls[-1]["url"] == "https://plane.example.com/api/github/acme/config/"

    def test_project_id_targets_project_tier(self, monkeypatch):
        mcp = _build_mcp()
        calls = _capture_requests(monkeypatch)
        _call_tool(mcp, "get_github_state_config", {"project_id": "proj-1"})
        assert calls[-1]["url"] == "https://plane.example.com/api/github/acme/projects/proj-1/config/"

    def test_instance_flag_targets_instance_tier(self, monkeypatch):
        mcp = _build_mcp()
        calls = _capture_requests(monkeypatch)
        _call_tool(mcp, "get_github_state_config", {"instance": True})
        assert calls[-1]["url"] == "https://plane.example.com/api/github/config/"

    def test_project_id_and_instance_is_rejected(self, monkeypatch):
        mcp = _build_mcp()
        calls = _capture_requests(monkeypatch)
        with pytest.raises(Exception, match="mutually exclusive"):
            _call_tool(mcp, "get_github_state_config", {"project_id": "proj-1", "instance": True})
        assert calls == []  # never reached the network


class TestSetGithubStateConfig:
    def test_puts_rules_to_project_tier(self, monkeypatch):
        mcp = _build_mcp()
        calls = _capture_requests(monkeypatch)
        _call_tool(
            mcp,
            "set_github_state_config",
            {"rules": {"pr_merged": "Done"}, "project_id": "proj-1"},
        )
        call = calls[-1]
        assert call["method"] == "PUT"
        assert call["url"] == "https://plane.example.com/api/github/acme/projects/proj-1/config/"
        assert call["json"] == {"rules": {"pr_merged": "Done"}}

    def test_rejects_unknown_event_key(self, monkeypatch):
        mcp = _build_mcp()
        calls = _capture_requests(monkeypatch)
        with pytest.raises(Exception, match="invalid event key"):
            _call_tool(mcp, "set_github_state_config", {"rules": {"pr_closed": "Done"}})
        assert calls == []  # client-side validation, never hit the network

    def test_rejects_empty_value(self, monkeypatch):
        mcp = _build_mcp()
        calls = _capture_requests(monkeypatch)
        with pytest.raises(Exception, match="non-empty string"):
            _call_tool(mcp, "set_github_state_config", {"rules": {"pr_merged": ""}})
        assert calls == []


class TestListWorkItemGithubLinks:
    def test_filters_and_parses_github_links(self, monkeypatch):
        mcp = _build_mcp()
        fake_results = [
            WorkItemLink(id="l1", url="https://github.com/acme/repo/pull/42", title="GitHub pr: 42"),
            WorkItemLink(
                id="l2",
                url="https://github.com/acme/repo/commit/abcd123",
                title="GitHub commit: abcd123",
            ),
            WorkItemLink(
                id="l3",
                url="https://github.com/acme/repo/tree/feature-x",
                title="GitHub branch: feature-x",
            ),
            WorkItemLink(id="l4", url="https://example.com/not-github", title="Some other link"),
            WorkItemLink(
                id="l5",
                url="https://github.com/acme/repo/issues/1",
                title="Manually pasted, no fork title",
            ),
        ]
        fake_response = PaginatedWorkItemLinkResponse(
            results=fake_results,
            next_cursor="",
            prev_cursor="",
            total_count=len(fake_results),
            count=len(fake_results),
            total_pages=1,
            total_results=len(fake_results),
            extra_stats=None,
            next_page_results=False,
            prev_page_results=False,
        )

        class _FakeLinks:
            @staticmethod
            def list(**kwargs):
                return fake_response

        class _FakeWorkItems:
            links = _FakeLinks()

        class _FakeClient:
            work_items = _FakeWorkItems()

        monkeypatch.setattr(
            "plane_mcp.tools.github_ext.get_plane_client_context",
            lambda: (_FakeClient(), "acme"),
        )

        result = _call_tool(
            mcp, "list_work_item_github_links", {"project_id": "p1", "work_item_id": "w1"}
        )

        items = result.structured_content["result"]
        assert len(items) == 4  # l4 (non-github) excluded
        by_id = {item["link_id"]: item for item in items}

        assert by_id["l1"] == {
            "link_type": "pr",
            "url": "https://github.com/acme/repo/pull/42",
            "external_id": "42",
            "link_id": "l1",
        }
        assert by_id["l2"]["link_type"] == "commit"
        assert by_id["l2"]["external_id"] == "abcd123"
        assert by_id["l3"]["link_type"] == "branch"
        assert by_id["l3"]["external_id"] == "feature-x"
        # github.com link whose title the fork didn't write -> unparsed, still returned
        assert by_id["l5"]["link_type"] is None
        assert by_id["l5"]["external_id"] is None
        assert "l4" not in by_id
