"""Tests for the silent-coercion and cycle-date defects in plane-mcp-server
(#53, #54, #55, #56, #41).

Each test here pins a FAILURE state: the bug is what makes it red, and it goes
green only when the fix lands. They are deliberately written so that a
fallback which swallows the bad input still fails — "no exception raised" is
never the assertion for the coercion cases, because the defect IS a silent
success.

No test contacts a live server; every SDK call is faked.
"""

import asyncio

import pytest
from fastmcp import FastMCP

from plane_mcp.client import PlaneClientContext

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _views_tool():
    from plane_mcp.tools.views_ext import register_views_ext_tools

    mcp = FastMCP("test")
    register_views_ext_tools(mcp)
    return asyncio.run(mcp.get_tool("list_workspace_view_issues"))


def _tool(module_path, register_fn, name):
    import importlib

    mod = importlib.import_module(module_path)
    mcp = FastMCP("test")
    getattr(mod, register_fn)(mcp)
    return asyncio.run(mcp.get_tool(name))


# ==========================================================================
# #53 + #54 — manage_cycle_archive date comparison
# ==========================================================================


class _RecordingCycles:
    """Records what the archive path actually SENT to the server."""

    def __init__(self, end_date):
        self._end_date = end_date
        self.update_calls: list[dict] = []
        self.archive_calls: int = 0

    def retrieve(self, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(end_date=self._end_date)

    def update(self, **kwargs):
        self.update_calls.append(kwargs)

    def archive(self, **kwargs):
        self.archive_calls += 1
        return True


class _FakeClient:
    def __init__(self, cycles):
        self.cycles = cycles


def _cycle_tool(monkeypatch, cycles):
    from plane_mcp.tools import cycles as cycles_mod

    def _ctx(workspace_slug=None, require_workspace=True):
        return PlaneClientContext(client=_FakeClient(cycles), workspace_slug="ws")

    monkeypatch.setattr(cycles_mod, "get_plane_client_context", _ctx)
    mcp = FastMCP("test")
    cycles_mod.register_cycle_tools(mcp)
    return asyncio.run(mcp.get_tool("manage_cycle_archive"))


def test_archive_sets_end_date_when_equal_to_today(monkeypatch):
    """#54: end_date == today is NOT in the past, so the update must happen.

    Pre-fix `end_date > today` is False here, the update is skipped, and the
    cycle is archived outside its own window — silently.
    """
    from datetime import date

    cycles = _RecordingCycles(end_date=date.today().isoformat())
    tool = _cycle_tool(monkeypatch, cycles)
    asyncio.run(tool.run({"project_id": "p", "cycle_id": "c", "archive": True}))

    assert cycles.update_calls, (
        "end_date == today skipped the update — the cycle is outside its window "
        "and Plane treats today's end_date as still active"
    )
    assert cycles.archive_calls == 1


def test_archive_moves_end_date_into_the_past(monkeypatch):
    """#54: the value sent must be strictly BEFORE today, else Plane 400s.

    Server rule (plane/app/views/cycle/archive.py):
        if cycle.end_date >= timezone.now(): 400 "Only completed cycles can be archived"
    and the write path anchors a bare date at 00:00 local, so `today` still
    compares >= now() for the rest of the day. Yesterday is the first value
    that always satisfies it.
    """
    from datetime import date

    today = date.today()
    cycles = _RecordingCycles(end_date=today.isoformat())
    tool = _cycle_tool(monkeypatch, cycles)
    asyncio.run(tool.run({"project_id": "p", "cycle_id": "c", "archive": True}))

    sent = cycles.update_calls[0]["data"].end_date
    assert sent < today.isoformat(), (
        f"sent end_date={sent!r} is not strictly in the past; Plane rejects "
        "archive unless end_date < now()"
    )


def test_archive_normalizes_iso_datetime_end_date(monkeypatch):
    """#53: a full ISO-8601 datetime must be compared as its DATE, not its text.

    "2026-09-17T00:00:00Z" > "2026-09-17" is True purely because 'T' sorts
    after end-of-string, so a cycle ending TODAY was read as ending in the
    future and its end_date was left stale.
    """
    from datetime import date

    today = date.today().isoformat()
    cycles = _RecordingCycles(end_date=f"{today}T00:00:00Z")
    tool = _cycle_tool(monkeypatch, cycles)
    asyncio.run(tool.run({"project_id": "p", "cycle_id": "c", "archive": True}))

    assert cycles.update_calls, (
        "ISO-datetime end_date was lexically 'greater' than today's bare date, "
        "so the update was skipped — the #53 defect"
    )


def test_archive_leaves_a_genuinely_future_cycle_alone(monkeypatch):
    """The other direction: a real future end_date must still be rewritten.

    Guards against an over-eager fix that rewrites unconditionally.
    """
    from datetime import date, timedelta

    future = (date.today() + timedelta(days=30)).isoformat()
    cycles = _RecordingCycles(end_date=future)
    tool = _cycle_tool(monkeypatch, cycles)
    asyncio.run(tool.run({"project_id": "p", "cycle_id": "c", "archive": True}))

    assert cycles.update_calls, "a future end_date must be moved into the past"


# ==========================================================================
# #55 — create_state / update_state group coercion
# ==========================================================================


@pytest.mark.parametrize("tool_name", ["create_state", "update_state"])
@pytest.mark.parametrize("bad", ["backlogs", "Backlog", "done", ""])
def test_state_rejects_an_invalid_group(monkeypatch, tool_name, bad):
    """#55: a misspelled group must raise, not become group=None.

    "" is included deliberately: a falsy value is what a calling model emits
    for 'unset', and accepting it as valid is the same silent corruption.
    """
    from plane_mcp.tools import states as states_mod

    def _ctx(workspace_slug=None, require_workspace=True):
        return PlaneClientContext(client=None, workspace_slug="ws")

    monkeypatch.setattr(states_mod, "get_plane_client_context", _ctx)
    mcp = FastMCP("test")
    states_mod.register_state_tools(mcp)
    tool = asyncio.run(mcp.get_tool(tool_name))

    kwargs = {"project_id": "p", "group": bad}
    if tool_name == "create_state":
        kwargs |= {"name": "n", "color": "#fff"}
    else:
        kwargs |= {"state_id": "s"}

    with pytest.raises(ValueError):
        asyncio.run(tool.run(kwargs))


@pytest.mark.parametrize("good", ["backlog", "unstarted", "started", "completed", "cancelled"])
def test_state_accepts_every_valid_group(monkeypatch, good):
    """The valid set must survive the tightened validation."""
    from types import SimpleNamespace

    from plane_mcp.tools import states as states_mod

    sent = {}

    class _States:
        def update(self, **kwargs):
            sent["group"] = kwargs["data"].group
            return SimpleNamespace(id="s")

    def _ctx(workspace_slug=None, require_workspace=True):
        return PlaneClientContext(client=SimpleNamespace(states=_States()), workspace_slug="ws")

    monkeypatch.setattr(states_mod, "get_plane_client_context", _ctx)
    mcp = FastMCP("test")
    states_mod.register_state_tools(mcp)
    tool = asyncio.run(mcp.get_tool("update_state"))
    asyncio.run(tool.run({"project_id": "p", "state_id": "s", "group": good}))

    assert sent["group"] == good


def test_state_none_group_still_means_unset(monkeypatch):
    """group=None is the documented 'leave alone' value and must not raise."""
    from types import SimpleNamespace

    from plane_mcp.tools import states as states_mod

    sent = {}

    class _States:
        def update(self, **kwargs):
            sent["group"] = kwargs["data"].group
            return SimpleNamespace(id="s")

    def _ctx(workspace_slug=None, require_workspace=True):
        return PlaneClientContext(client=SimpleNamespace(states=_States()), workspace_slug="ws")

    monkeypatch.setattr(states_mod, "get_plane_client_context", _ctx)
    mcp = FastMCP("test")
    states_mod.register_state_tools(mcp)
    tool = asyncio.run(mcp.get_tool("update_state"))
    asyncio.run(tool.run({"project_id": "p", "state_id": "s", "group": None}))

    assert sent["group"] is None


# ==========================================================================
# #56 — create_work_item / update_work_item priority coercion
# ==========================================================================


@pytest.mark.parametrize("tool_name", ["create_work_item", "update_work_item"])
@pytest.mark.parametrize("bad", ["Critical", "HIGH", "p1", "urgent "])
def test_work_item_rejects_an_invalid_priority(monkeypatch, tool_name, bad):
    """#56: 'Critical' must raise, not silently become the server default.

    Pre-fix the caller got HTTP 200 and priority "none" — the payload never
    carried the value they asked for and nothing said so.
    """
    from plane_mcp.tools import work_items as wi

    def _ctx(workspace_slug=None, require_workspace=True):
        return PlaneClientContext(client=None, workspace_slug="ws")

    monkeypatch.setattr(wi, "get_plane_client_context", _ctx)
    mcp = FastMCP("test")
    wi.register_work_item_tools(mcp)
    tool = asyncio.run(mcp.get_tool(tool_name))

    kwargs = {"project_id": "p", "priority": bad}
    if tool_name == "create_work_item":
        kwargs |= {"name": "n"}
    else:
        kwargs |= {"work_item_id": "w"}

    with pytest.raises(ValueError):
        asyncio.run(tool.run(kwargs))


@pytest.mark.parametrize("good", ["urgent", "high", "medium", "low", "none"])
def test_work_item_accepts_every_valid_priority(monkeypatch, good):
    """The valid set must survive the tightened validation."""
    from types import SimpleNamespace

    from plane_mcp.tools import work_items as wi

    sent = {}

    class _WI:
        def update(self, **kwargs):
            sent["priority"] = kwargs["data"].priority
            return SimpleNamespace(id="w")

    def _ctx(workspace_slug=None, require_workspace=True):
        return PlaneClientContext(client=SimpleNamespace(work_items=_WI()), workspace_slug="ws")

    monkeypatch.setattr(wi, "get_plane_client_context", _ctx)
    mcp = FastMCP("test")
    wi.register_work_item_tools(mcp)
    tool = asyncio.run(mcp.get_tool("update_work_item"))
    asyncio.run(tool.run({"project_id": "p", "work_item_id": "w", "priority": good}))

    assert sent["priority"] == good


def test_work_item_none_priority_still_means_unset(monkeypatch):
    """priority=None must keep meaning 'leave alone'."""
    from types import SimpleNamespace

    from plane_mcp.tools import work_items as wi

    sent = {}

    class _WI:
        def update(self, **kwargs):
            sent["priority"] = kwargs["data"].priority
            return SimpleNamespace(id="w")

    def _ctx(workspace_slug=None, require_workspace=True):
        return PlaneClientContext(client=SimpleNamespace(work_items=_WI()), workspace_slug="ws")

    monkeypatch.setattr(wi, "get_plane_client_context", _ctx)
    mcp = FastMCP("test")
    wi.register_work_item_tools(mcp)
    tool = asyncio.run(mcp.get_tool("update_work_item"))
    asyncio.run(tool.run({"project_id": "p", "work_item_id": "w", "priority": None}))

    assert sent["priority"] is None


# ==========================================================================
# #41 — list_workspace_view_issues must accept a per-call workspace_slug
# ==========================================================================


def test_views_ext_forwards_workspace_slug(monkeypatch):
    """#41: the tool must thread an explicit slug through, like its 23 siblings.

    Pre-fix the signature has no such parameter, so the ContextVar-scoped
    session default is the only source and a fresh HTTP request cannot see it.
    """
    from plane_mcp.tools import views_ext

    seen = {}

    def _ctx(workspace_slug=None, require_workspace=True):
        seen["slug"] = workspace_slug
        from types import SimpleNamespace

        config = SimpleNamespace(base_path="https://x/api/v1", api_key="k", access_token=None)
        return PlaneClientContext(
            client=SimpleNamespace(config=config),
            workspace_slug=workspace_slug or "ws",
        )

    monkeypatch.setattr(views_ext, "get_plane_client_context", _ctx)
    monkeypatch.setattr(views_ext, "_send", lambda *a, **k: {"results": []})

    mcp = FastMCP("test")
    views_ext.register_views_ext_tools(mcp)
    tool = asyncio.run(mcp.get_tool("list_workspace_view_issues"))

    asyncio.run(tool.run({"workspace_slug": "cocos"}))
    assert seen["slug"] == "cocos", (
        "the per-call workspace_slug was dropped — callers cannot route around "
        "a ContextVar-scoped session default"
    )


def test_views_ext_declares_the_parameter():
    """Cheap structural pin: the schema must advertise workspace_slug."""
    tool = _views_tool()
    props = tool.parameters.get("properties", {})
    assert "workspace_slug" in props, f"workspace_slug missing from {sorted(props)}"
