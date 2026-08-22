"""Tests for `clear=[...]` on `update_project` / `update_work_item`
(plane-mcp-server#21).

The bug this guards is a SILENT no-op: plane-sdk serialises update payloads with
`model_dump(exclude_none=True)`, so `default_assignee=None` never reaches the wire
and the PATCH returns 200 with the field unchanged. Every test here therefore
asserts on the PAYLOAD ACTUALLY SENT — asserting only that the tool returned
successfully is exactly the check that could not fail while the bug was live.

All HTTP and SDK calls are mocked; nothing here contacts a live server.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from plane_mcp.clearing import build_clear_payload
from plane_mcp.client import PlaneClientContext
from plane_mcp.tools.projects import register_project_tools
from plane_mcp.tools.work_items import register_work_item_tools


class _FakeConfig:
    def __init__(self):
        self.base_path = "https://plane.the1studio.org/api/v1"
        self.api_key = "test-api-key"
        self.access_token = None


class _RecordingResource:
    """Mimics `client.projects` / `client.work_items` — records SDK-path calls."""

    def __init__(self):
        self.update_calls: list[dict] = []

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        return SimpleNamespace(id="sdk-path")


class _FakeClient:
    def __init__(self):
        self.config = _FakeConfig()
        self.projects = _RecordingResource()
        self.work_items = _RecordingResource()


def _tool(register, name):
    """Register the tools on a throwaway server and return one by name."""
    mcp = FastMCP("test")
    register(mcp)
    return asyncio.run(mcp.get_tool(name))


def _call(tool, **kwargs):
    return asyncio.run(tool.run(kwargs))


@pytest.fixture
def client(monkeypatch):
    fake = _FakeClient()

    def _fake(workspace_slug=None, require_workspace=True):
        return PlaneClientContext(
            client=fake, workspace_slug=workspace_slug or "test-workspace"
        )

    # Patch at the USE site: both tool modules did `from plane_mcp.client import
    # get_plane_client_context`, so rebinding the name in plane_mcp.client alone
    # would leave the already-imported references untouched.
    monkeypatch.setattr("plane_mcp.tools.projects.get_plane_client_context", _fake)
    monkeypatch.setattr("plane_mcp.tools.work_items.get_plane_client_context", _fake)
    return fake


# --------------------------------------------------------------------------
# build_clear_payload — the unit the tools delegate to
# --------------------------------------------------------------------------


class _Model(SimpleNamespace):
    pass


def _update_project_model(**kwargs):
    from plane.models.projects import UpdateProject

    return UpdateProject(**kwargs)


def test_clear_puts_an_explicit_null_in_the_payload():
    """The whole point: the key must be PRESENT and null, not absent."""
    payload = build_clear_payload(
        _update_project_model(), ["default_assignee"], "update_project"
    )
    assert "default_assignee" in payload, (
        "the key was dropped — this is the exact silent no-op #21 is about"
    )
    assert payload["default_assignee"] is None


def test_clear_preserves_other_values_set_in_the_same_call():
    payload = build_clear_payload(
        _update_project_model(name="Renamed"), ["default_assignee"], "update_project"
    )
    assert payload["name"] == "Renamed"
    assert payload["default_assignee"] is None


def test_clear_does_not_smuggle_in_unset_fields_as_null():
    """Only the NAMED fields become null; everything else stays omitted, so a
    clear never silently wipes a field the caller did not mention."""
    payload = build_clear_payload(
        _update_project_model(), ["default_assignee"], "update_project"
    )
    assert set(payload) == {"default_assignee"}


def test_unknown_field_name_raises_rather_than_clearing_nothing():
    with pytest.raises(ValueError, match="unknown field"):
        build_clear_payload(
            _update_project_model(), ["defaultAssignee"], "update_project"
        )


def test_value_and_clear_for_the_same_field_raises():
    with pytest.raises(ValueError, match="also listed in"):
        build_clear_payload(
            _update_project_model(default_assignee="some-uuid"),
            ["default_assignee"],
            "update_project",
        )


def test_multiple_fields_clear_together():
    payload = build_clear_payload(
        _update_project_model(), ["default_assignee", "project_lead"], "update_project"
    )
    assert payload == {"default_assignee": None, "project_lead": None}


# --------------------------------------------------------------------------
# update_project — dispatch
# --------------------------------------------------------------------------


def test_update_project_with_clear_sends_a_raw_patch_carrying_the_null(client, monkeypatch):
    sent: dict = {}

    def _fake_send(c, method, path, json=None, params=None):
        sent.update(method=method, path=path, json=json)
        return {"id": "11111111-1111-1111-1111-111111111111", "name": "P", "identifier": "P"}

    monkeypatch.setattr("plane_mcp.tools.projects._send", _fake_send)

    tool = _tool(register_project_tools, "update_project")
    _call(tool, project_id="pid", clear=["default_assignee"])

    assert sent["method"] == "PATCH"
    assert sent["path"] == "/workspaces/test-workspace/projects/pid/"
    assert sent["json"] == {"default_assignee": None}
    assert client.projects.update_calls == [], (
        "must NOT fall through to the SDK — that is the path that drops the null"
    )


def test_update_project_without_clear_is_byte_for_byte_the_old_sdk_path(client, monkeypatch):
    """The pre-existing behaviour must be untouched for every existing caller."""

    def _explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("raw PATCH used when no clear was requested")

    monkeypatch.setattr("plane_mcp.tools.projects._send", _explode)

    tool = _tool(register_project_tools, "update_project")
    _call(tool, project_id="pid", name="Renamed")

    assert len(client.projects.update_calls) == 1
    assert client.projects.update_calls[0]["data"].name == "Renamed"


def test_update_project_empty_clear_list_is_not_a_clear(client, monkeypatch):
    def _explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("empty clear list took the raw-PATCH path")

    monkeypatch.setattr("plane_mcp.tools.projects._send", _explode)

    tool = _tool(register_project_tools, "update_project")
    _call(tool, project_id="pid", clear=[], name="Renamed")

    assert len(client.projects.update_calls) == 1


# --------------------------------------------------------------------------
# update_work_item — dispatch
# --------------------------------------------------------------------------


def test_update_work_item_with_clear_sends_a_raw_patch_carrying_the_null(client, monkeypatch):
    sent: dict = {}

    def _fake_send(c, method, path, json=None, params=None):
        sent.update(method=method, path=path, json=json)
        return {"id": "22222222-2222-2222-2222-222222222222", "name": "W"}

    monkeypatch.setattr("plane_mcp.tools.work_items._api_send", _fake_send)

    tool = _tool(register_work_item_tools, "update_work_item")
    _call(tool, project_id="pid", work_item_id="wid", clear=["target_date"])

    assert sent["method"] == "PATCH"
    assert sent["path"] == "/workspaces/test-workspace/projects/pid/issues/wid/"
    assert sent["json"] == {"target_date": None}
    assert client.work_items.update_calls == []


def test_update_work_item_without_clear_is_byte_for_byte_the_old_sdk_path(client, monkeypatch):
    def _explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("raw PATCH used when no clear was requested")

    monkeypatch.setattr("plane_mcp.tools.work_items._api_send", _explode)

    tool = _tool(register_work_item_tools, "update_work_item")
    _call(tool, project_id="pid", work_item_id="wid", name="Renamed")

    assert len(client.work_items.update_calls) == 1
