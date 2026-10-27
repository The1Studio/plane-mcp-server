"""Shims for plane-sdk response types narrower than the API payload (#36, #37).

The HTTP layer (`requests.Session.get`) is mocked with the exact failing payload
shapes from the issues; the SDK resources, URL building and model validation run
for real.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import FastMCP
from plane.client import PlaneClient
from plane.models.work_items import WorkItemDetail, WorkItemRelationResponse

from plane_mcp.tools.work_item_relations import register_work_item_relation_tools
from plane_mcp.tools.work_items import register_work_item_tools

PROJECT = "7b73db23-7356-4b84-9314-b7bc21892b3d"
ITEM = "6241518c-f8ab-4811-9e45-1733187416d7"
OTHER = "11111111-2222-3333-4444-555555555555"


def _response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"x"
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = payload
    return resp


def _call(register, tool, payload, **kwargs):
    client = PlaneClient(base_url="https://plane.test", api_key="k")
    mcp = FastMCP("t")
    register(mcp)
    with (
        patch("requests.Session.get", return_value=_response(payload)) as get,
        patch(
            f"plane_mcp.tools.{register.__module__.rsplit('.', 1)[-1]}.get_plane_client_context",
            return_value=(client, "ws"),
        ),
    ):
        result = asyncio.run(mcp.call_tool(tool, kwargs))
    return result, get


RELATIONS_PAYLOAD = {
    "blocking": [],
    "blocked_by": [{"project_id": PROJECT, "issue_id": OTHER}],
    "duplicate": [],
    "relates_to": [OTHER],
    "start_after": [],
    "start_before": [],
    "finish_after": [],
    "finish_before": [],
}


def test_sdk_still_rejects_relation_objects():
    """Documents WHY the shim exists; if this fails the SDK was fixed, drop the shim."""
    with pytest.raises(Exception, match="blocked_by"):
        WorkItemRelationResponse.model_validate(RELATIONS_PAYLOAD)


def test_list_relations_accepts_object_entries():
    result, get = _call(
        register_work_item_relation_tools,
        "list_work_item_relations",
        RELATIONS_PAYLOAD,
        project_id=PROJECT,
        work_item_id=ITEM,
    )
    data = result.structured_content
    assert data["blocked_by"] == [{"project_id": PROJECT, "issue_id": OTHER}]
    assert data["relates_to"] == [OTHER]
    assert get.call_args.args[0].endswith(f"/work-items/{ITEM}/relations/")


def test_list_relations_tolerates_omitted_keys():
    result, _ = _call(
        register_work_item_relation_tools,
        "list_work_item_relations",
        {"blocked_by": [{"project_id": PROJECT, "issue_id": OTHER}]},
        project_id=PROJECT,
        work_item_id=ITEM,
    )
    assert result.structured_content["blocking"] == []


DETAIL_EXPANDED = {
    "id": ITEM,
    "sequence_id": 7,
    "created_by": {"id": OTHER, "display_name": "Ann"},
}


def test_sdk_still_rejects_expanded_created_by():
    with pytest.raises(Exception, match="created_by"):
        WorkItemDetail.model_validate(DETAIL_EXPANDED)


def test_retrieve_with_expand_created_by_returns_object():
    result, get = _call(
        register_work_item_tools,
        "retrieve_work_item",
        DETAIL_EXPANDED,
        project_id=PROJECT,
        work_item_id=ITEM,
        expand="created_by",
        fields="id,sequence_id,created_by",
    )
    assert result.structured_content["created_by"]["display_name"] == "Ann"
    assert get.call_args.kwargs["params"]["expand"] == "created_by"
    assert get.call_args.kwargs["params"]["fields"] == "id,sequence_id,created_by"


def test_retrieve_without_expand_keeps_sdk_path():
    result, _ = _call(
        register_work_item_tools,
        "retrieve_work_item",
        {"id": ITEM, "created_by": OTHER},
        project_id=PROJECT,
        work_item_id=ITEM,
    )
    assert result.structured_content["created_by"] == OTHER


def test_http_error_still_propagates():
    client = PlaneClient(base_url="https://plane.test", api_key="k")
    mcp = FastMCP("t")
    register_work_item_relation_tools(mcp)
    with (
        patch("requests.Session.get", return_value=_response({"error": "x"}, 404)),
        patch("plane_mcp.tools.work_item_relations.get_plane_client_context", return_value=(client, "ws")),
        pytest.raises(Exception, match="404"),
    ):
        asyncio.run(mcp.call_tool("list_work_item_relations", {"project_id": PROJECT, "work_item_id": ITEM}))
