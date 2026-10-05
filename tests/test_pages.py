"""Tests for the project-page tools (`list_pages`, `retrieve_page`, `update_page`).

Project pages are served by the fork's `page_ext` endpoints
(`/api/v1/workspaces/<slug>/projects/<id>/pages/[<page_id>/]`); core Plane's v1
API has no such route on this build, so the plane-sdk's `*_project_page` methods
404 and the tools call the endpoints through the shared `_send` helper instead
(same pattern as `tests/test_project_admin.py`). All HTTP is mocked with real
`httpx.Response` objects; nothing here calls a live server.

Deliberately exercises the FAILURE states: a server without the endpoints must
raise an actionable `RuntimeError` (never an empty list, which would misread as
"this project has no pages"), while a missing page, a locked page and a 403 must
keep their real status and reason.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP

from plane_mcp.client import PlaneClientContext
from plane_mcp.tools.pages import register_page_tools

_REQUEST = httpx.Request("GET", "https://plane.the1studio.org/api/v1/")
_BASE = "https://plane.the1studio.org/api/v1"
_PROJECT = "90e6d09c-804d-4da9-be98-a47ebe17e7b6"
_PAGE = "6dd6c1ac-274b-4c07-b735-c70e195f495c"


class _FakeConfig:
    """Mimics the subset of plane-sdk's client.config that `_send` reads."""

    def __init__(self):
        self.base_path = _BASE
        self.api_key = "test-api-key"
        self.access_token = None


class _ExplodingPages:
    """The SDK page methods must NOT be used for project pages: they target a
    route core does not have. Any call fails the test loudly."""

    def list_project_pages(self, **_kwargs):
        raise AssertionError("project pages must not go through the SDK")

    def retrieve_project_page(self, **_kwargs):
        raise AssertionError("project pages must not go through the SDK")

    def list_workspace_pages(self, workspace_slug, params=None):
        return SimpleNamespace(results=[f"workspace-page-of-{workspace_slug}"])

    def retrieve_workspace_page(self, workspace_slug, page_id):
        return f"workspace-page-{page_id}"


class _FakeClient:
    def __init__(self):
        self.config = _FakeConfig()
        self.pages = _ExplodingPages()


def _get_tool_fn(mcp: FastMCP, name: str):
    """The registered tool's underlying sync callable (only the lookup is async)."""
    return asyncio.run(mcp.get_tool(name)).fn


@pytest.fixture()
def mcp():
    m = FastMCP("test")
    register_page_tools(m)
    return m


def _stub_context(monkeypatch, client, default_slug="cocos"):
    def _fake(workspace_slug=None):
        return PlaneClientContext(client=client, workspace_slug=workspace_slug or default_slug)

    monkeypatch.setattr("plane_mcp.tools.pages.get_plane_client_context", _fake)


def _mock_response(status_code: int, payload=None) -> httpx.Response:
    return httpx.Response(status_code, json=payload if payload is not None else {}, request=_REQUEST)


def _stub_http(monkeypatch, status_code: int, payload=None) -> dict:
    """Patch httpx.request as `_send` calls it; returns the captured request."""
    captured: dict = {}

    def _fake_request(method, url, headers=None, json=None, params=None, timeout=None):
        captured.update(method=method, url=url, json=json, headers=headers)
        return _mock_response(status_code, payload)

    monkeypatch.setattr("plane_mcp.tools.workload.httpx.request", _fake_request)
    return captured


class TestRegistration:
    def test_page_tools_are_registered_once_each(self, mcp):
        tools = asyncio.run(mcp.list_tools())
        names = [tool.name for tool in tools]
        for expected in ("list_pages", "retrieve_page", "update_page", "create_page"):
            assert names.count(expected) == 1, f"{expected} registered {names.count(expected)} times"


class TestListPages:
    def test_project_pages_use_the_fork_endpoint_not_the_sdk(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())
        rows = [{"id": _PAGE, "name": "Plan", "is_locked": False, "is_archived": False}]
        captured = _stub_http(monkeypatch, 200, rows)

        result = _get_tool_fn(mcp, "list_pages")(project_id=_PROJECT)

        assert captured["method"] == "GET"
        assert captured["url"] == f"{_BASE}/workspaces/cocos/projects/{_PROJECT}/pages/"
        assert captured["headers"]["X-Api-Key"] == "test-api-key"
        assert result == rows

    def test_honours_per_call_workspace_slug(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient(), default_slug="unity")
        captured = _stub_http(monkeypatch, 200, [])

        _get_tool_fn(mcp, "list_pages")(project_id=_PROJECT, workspace_slug="cocos")

        assert "/workspaces/cocos/projects/" in captured["url"]

    def test_workspace_pages_are_unchanged(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())

        assert _get_tool_fn(mcp, "list_pages")() == ["workspace-page-of-cocos"]

    def test_server_without_the_endpoints_raises_actionable_error_not_empty_list(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())
        _stub_http(monkeypatch, 404, {"error": "Page not found."})  # Plane's unrouted-path 404 body

        with pytest.raises(RuntimeError, match="page_ext"):
            _get_tool_fn(mcp, "list_pages")(project_id=_PROJECT)

    def test_403_keeps_its_status_and_gains_the_reason(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())
        _stub_http(monkeypatch, 403, {"detail": "You do not have permission to perform this action."})

        with pytest.raises(httpx.HTTPStatusError, match="do not have permission") as excinfo:
            _get_tool_fn(mcp, "list_pages")(project_id=_PROJECT)
        assert excinfo.value.response.status_code == 403


class TestRetrievePage:
    def test_project_page_returns_html_and_plain_text(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())
        page = {"id": _PAGE, "name": "Plan", "description_html": "<table></table>", "description_stripped": ""}
        captured = _stub_http(monkeypatch, 200, page)

        result = _get_tool_fn(mcp, "retrieve_page")(page_id=_PAGE, project_id=_PROJECT)

        assert captured["method"] == "GET"
        assert captured["url"] == f"{_BASE}/workspaces/cocos/projects/{_PROJECT}/pages/{_PAGE}/"
        assert result["description_html"] == "<table></table>"

    def test_workspace_page_is_unchanged(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())

        assert _get_tool_fn(mcp, "retrieve_page")(page_id=_PAGE) == f"workspace-page-{_PAGE}"

    def test_missing_page_is_a_404_not_the_fork_unavailable_error(self, monkeypatch, mcp):
        """The fork answers a missing page with its own {"error": ...} message. Only
        the unrouted-path body means "server lacks the endpoints"."""
        _stub_context(monkeypatch, _FakeClient())
        _stub_http(monkeypatch, 404, {"error": "Page does not exist in this project"})

        with pytest.raises(httpx.HTTPStatusError, match="Page does not exist in this project") as excinfo:
            _get_tool_fn(mcp, "retrieve_page")(page_id=_PAGE, project_id=_PROJECT)
        assert excinfo.value.response.status_code == 404

    def test_server_without_the_endpoints_raises_actionable_error(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())
        _stub_http(monkeypatch, 404, {"error": "Page not found."})

        with pytest.raises(RuntimeError, match="page_ext"):
            _get_tool_fn(mcp, "retrieve_page")(page_id=_PAGE, project_id=_PROJECT)


class TestUpdatePage:
    def test_requires_name_or_description_html(self, mcp):
        with pytest.raises(ValueError, match="name and/or description_html"):
            _get_tool_fn(mcp, "update_page")(project_id=_PROJECT, page_id=_PAGE)

    def test_sends_only_the_provided_fields_as_a_patch(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())
        updated = {"id": _PAGE, "description_html": "<p>new</p>", "changed": True, "description_binary_cleared": True}
        captured = _stub_http(monkeypatch, 200, updated)

        result = _get_tool_fn(mcp, "update_page")(project_id=_PROJECT, page_id=_PAGE, description_html="<p>new</p>")

        assert captured["method"] == "PATCH"
        assert captured["url"] == f"{_BASE}/workspaces/cocos/projects/{_PROJECT}/pages/{_PAGE}/"
        assert captured["json"] == {"description_html": "<p>new</p>"}  # no name key, no nulls
        assert result["changed"] is True

    def test_name_only_and_both_fields(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())
        captured = _stub_http(monkeypatch, 200, {"id": _PAGE})
        fn = _get_tool_fn(mcp, "update_page")

        fn(project_id=_PROJECT, page_id=_PAGE, name="Renamed")
        assert captured["json"] == {"name": "Renamed"}

        fn(project_id=_PROJECT, page_id=_PAGE, name="Renamed", description_html="<p>x</p>")
        assert captured["json"] == {"name": "Renamed", "description_html": "<p>x</p>"}

    def test_empty_string_name_is_sent_not_dropped(self, monkeypatch, mcp):
        """`is not None`, not truthiness: a caller clearing the title must reach the server."""
        _stub_context(monkeypatch, _FakeClient())
        captured = _stub_http(monkeypatch, 200, {"id": _PAGE})

        _get_tool_fn(mcp, "update_page")(project_id=_PROJECT, page_id=_PAGE, name="")

        assert captured["json"] == {"name": ""}

    def test_honours_per_call_workspace_slug(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient(), default_slug="unity")
        captured = _stub_http(monkeypatch, 200, {"id": _PAGE})

        _get_tool_fn(mcp, "update_page")(project_id=_PROJECT, page_id=_PAGE, name="x", workspace_slug="cocos")

        assert "/workspaces/cocos/projects/" in captured["url"]

    @pytest.mark.parametrize(
        "status_code, payload, reason",
        [
            (423, {"error_code": 4701, "error_message": "PAGE_LOCKED", "error": "PAGE_LOCKED"}, "PAGE_LOCKED"),
            (409, {"error_code": 4702, "error_message": "PAGE_ARCHIVED", "error": "PAGE_ARCHIVED"}, "PAGE_ARCHIVED"),
            (400, {"error": "unsupported field(s): access"}, "unsupported field"),
        ],
    )
    def test_refusals_surface_status_and_reason(self, monkeypatch, mcp, status_code, payload, reason):
        _stub_context(monkeypatch, _FakeClient())
        _stub_http(monkeypatch, status_code, payload)

        with pytest.raises(httpx.HTTPStatusError, match=reason) as excinfo:
            _get_tool_fn(mcp, "update_page")(project_id=_PROJECT, page_id=_PAGE, name="x")
        assert excinfo.value.response.status_code == status_code

    def test_403_gains_the_reason(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())
        _stub_http(monkeypatch, 403, {"detail": "You do not have permission to perform this action."})

        with pytest.raises(httpx.HTTPStatusError, match="do not have permission"):
            _get_tool_fn(mcp, "update_page")(project_id=_PROJECT, page_id=_PAGE, name="x")

    def test_server_without_the_endpoints_raises_actionable_error(self, monkeypatch, mcp):
        _stub_context(monkeypatch, _FakeClient())
        _stub_http(monkeypatch, 404, {"error": "Page not found."})

        with pytest.raises(RuntimeError, match="page_ext"):
            _get_tool_fn(mcp, "update_page")(project_id=_PROJECT, page_id=_PAGE, name="x")
