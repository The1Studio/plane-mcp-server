"""Tests for the string/object duality of `assignees` and `labels`
(plane-mcp-server#27).

The API returns these as bare UUID strings unless the request asked for
`?expand=`. plane-sdk < 0.2.20 typed them as expanded objects only, so
`retrieve_work_item` raised a ValidationError for every assigned work item; the
pin bump to 0.2.20 fixes that, and in doing so lets the STRING form reach the
read-modify-write tools below for the first time.

What these tests actually guard is not the crash. `manage_work_item_assignee`
and `manage_work_item_label` rewrite the WHOLE relation list from what they
read, so a helper that quietly returned `[]` for the string form would strip
every existing assignee off a work item while reporting success. Each test
therefore asserts on the PAYLOAD SENT to `update`, never on the call returning.

All SDK calls are mocked; nothing here contacts a live server.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from plane_mcp.client import PlaneClientContext
from plane_mcp.tools.work_items import _relation_ids, register_work_item_tools

USER_A = "aaaaaaaa-0000-0000-0000-000000000001"
USER_B = "bbbbbbbb-0000-0000-0000-000000000002"
LABEL_A = "cccccccc-0000-0000-0000-000000000003"
LABEL_B = "dddddddd-0000-0000-0000-000000000004"


# --------------------------------------------------------------------------
# _relation_ids — the unit both tools delegate to
# --------------------------------------------------------------------------


def test_string_form_survives():
    """The shape a bare retrieve actually returns. Pre-fix this raised."""
    assert _relation_ids([USER_A, USER_B]) == [USER_A, USER_B]


def test_object_form_still_works():
    """`?expand=assignees` keeps returning UserLite; it must not regress."""
    assert _relation_ids(
        [SimpleNamespace(id=USER_A), SimpleNamespace(id=USER_B)]
    ) == [USER_A, USER_B]


def test_mixed_and_empty_entries_are_skipped_not_fatal():
    assert _relation_ids([USER_A, SimpleNamespace(id=USER_B), SimpleNamespace(id=None), ""]) == [
        USER_A,
        USER_B,
    ]


def test_none_and_empty_are_empty():
    assert _relation_ids(None) == []
    assert _relation_ids([]) == []


# --------------------------------------------------------------------------
# The tools — assert on the payload, because "it returned" cannot fail
# --------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self):
        self.base_path = "https://plane.the1studio.org/api/v1"
        self.api_key = "test-api-key"
        self.access_token = None


class _RecordingWorkItems:
    """Retrieve hands back the STRING form, exactly as the live API does."""

    def __init__(self, assignees, labels):
        self._assignees = assignees
        self._labels = labels
        self.update_calls: list[dict] = []

    def retrieve(self, **kwargs):
        return SimpleNamespace(assignees=self._assignees, labels=self._labels)

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        return SimpleNamespace(id="updated")


class _FakeClient:
    def __init__(self, assignees=(), labels=()):
        self.config = _FakeConfig()
        self.work_items = _RecordingWorkItems(list(assignees), list(labels))


def _tool(name):
    mcp = FastMCP("test")
    register_work_item_tools(mcp)
    return asyncio.run(mcp.get_tool(name))


def _call(tool, **kwargs):
    return asyncio.run(tool.run(kwargs))


def _patch(monkeypatch, fake):
    def _ctx(workspace_slug=None, require_workspace=True):
        return PlaneClientContext(client=fake, workspace_slug=workspace_slug or "test-workspace")

    monkeypatch.setattr("plane_mcp.tools.work_items.get_plane_client_context", _ctx)


@pytest.mark.parametrize(
    "tool_name,relation,existing,add_kw,add_value,keep",
    [
        ("manage_work_item_assignee", "assignees", [USER_A], "add_user_id", USER_B, USER_A),
        ("manage_work_item_label", "labels", [LABEL_A], "add_label_id", LABEL_B, LABEL_A),
    ],
)
def test_add_preserves_the_existing_string_entry(
    monkeypatch, tool_name, relation, existing, add_kw, add_value, keep
):
    """The regression that matters: adding one must not drop the other.

    With the pre-fix `[u.id for u in ...]` this raised AttributeError; with a
    lenient version that swallowed it, the payload would carry ONLY the added
    id and the PATCH would silently unassign `keep`.
    """
    fake = _FakeClient(**{relation: existing})
    _patch(monkeypatch, fake)
    _call(_tool(tool_name), project_id="p", work_item_id="w", **{add_kw: add_value})

    assert len(fake.work_items.update_calls) == 1
    sent = getattr(fake.work_items.update_calls[0]["data"], relation)
    assert keep in sent, f"{keep} was dropped — the read-modify-write lost an existing entry"
    assert add_value in sent
    assert len(sent) == 2


@pytest.mark.parametrize(
    "tool_name,relation,existing,remove_kw,remove_value,keep",
    [
        ("manage_work_item_assignee", "assignees", [USER_A, USER_B], "remove_user_id", USER_A, USER_B),
        ("manage_work_item_label", "labels", [LABEL_A, LABEL_B], "remove_label_id", LABEL_A, LABEL_B),
    ],
)
def test_remove_takes_only_the_named_entry(
    monkeypatch, tool_name, relation, existing, remove_kw, remove_value, keep
):
    fake = _FakeClient(**{relation: existing})
    _patch(monkeypatch, fake)
    _call(_tool(tool_name), project_id="p", work_item_id="w", **{remove_kw: remove_value})

    sent = getattr(fake.work_items.update_calls[0]["data"], relation)
    assert sent == [keep]


def test_add_is_idempotent_on_an_already_assigned_user(monkeypatch):
    fake = _FakeClient(assignees=[USER_A])
    _patch(monkeypatch, fake)
    _call(_tool("manage_work_item_assignee"), project_id="p", work_item_id="w", add_user_id=USER_A)

    assert fake.work_items.update_calls[0]["data"].assignees == [USER_A]
