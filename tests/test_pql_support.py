"""Tests for PQL capability detection (`plane_mcp.pql_support`).

The bug these guard against (issue #32) is a SILENT one: a deployment that does
not implement PQL answers a `pql=`-bearing request with HTTP 200 and the
UNFILTERED rows, so the caller cannot tell a filtered answer from an unfiltered
one. Every assertion below therefore pins a failure-shaped state — the probe
being accepted, the refusal payload being returned, the inconclusive probe NOT
being cached — because a test that only pinned the happy path would stay green
through exactly the regression that matters.

Nothing here touches a live server; the Plane client is a stub.
"""

from types import SimpleNamespace

import pytest
from plane.errors import HttpError

from plane_mcp.pql_support import (
    PQL_PROBE_EXPRESSION,
    cached_pql_support,
    guard_pql,
    probe_pql_support,
    reset_pql_support_cache,
)

WORKSPACE = "acme"


class StubWorkItems:
    """Records probe calls and replays a scripted outcome per route."""

    def __init__(self, outcome, project_outcome=None):
        self.outcome = outcome
        self.project_outcome = project_outcome if project_outcome is not None else outcome
        self.calls = []

    @staticmethod
    def _replay(outcome):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def list_workspace(self, *, workspace_slug, params):
        self.calls.append(("workspace", workspace_slug, params))
        return self._replay(self.outcome)

    def list(self, *, workspace_slug, project_id, params):
        self.calls.append(("project", workspace_slug, params))
        return self._replay(self.project_outcome)


def make_client(outcome, base_url="https://plane.example.com", project_outcome=None):
    return SimpleNamespace(
        config=SimpleNamespace(base_path=base_url),
        work_items=StubWorkItems(outcome, project_outcome),
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_pql_support_cache()
    yield
    reset_pql_support_cache()


# --- the silent-ignore case: a 200 for an unparseable expression ------------


def test_probe_reports_unsupported_when_malformed_pql_is_accepted():
    """HTTP 200 for an expression no parser accepts means `pql` was ignored."""
    client = make_client(SimpleNamespace(results=[], total_count=135))
    assert probe_pql_support(client, WORKSPACE) is False


def test_probe_sends_a_deliberately_unparseable_expression():
    """A probe a real parser might ACCEPT would report every server as supporting PQL."""
    client = make_client(SimpleNamespace(results=[], total_count=135))
    probe_pql_support(client, WORKSPACE)

    (_, _, params) = client.work_items.calls[0]
    assert params.pql == PQL_PROBE_EXPRESSION
    # Unbalanced parens plus a trailing operator — not accidentally valid.
    assert "(((" in params.pql
    assert params.pql.rstrip().endswith("AND")


def test_guard_refuses_instead_of_returning_unfiltered_rows():
    client = make_client(SimpleNamespace(results=[], total_count=135))
    err = guard_pql(client, WORKSPACE, 'assignee = "abc"', "list_work_items")

    assert err is not None
    assert err["pql_supported"] is False
    assert err["failed_pql"] == 'assignee = "abc"'
    assert err["tool"] == "list_work_items"
    assert "UNFILTERED" in err["error"]


# --- the supported case ----------------------------------------------------


def test_probe_reports_supported_when_malformed_pql_is_rejected():
    client = make_client(HttpError("bad pql", 400, {"pql": "syntax error"}))
    assert probe_pql_support(client, WORKSPACE) is True


def test_guard_lets_the_call_through_when_pql_is_supported():
    client = make_client(HttpError("bad pql", 400, {"pql": "syntax error"}))
    assert guard_pql(client, WORKSPACE, 'assignee = "abc"', "list_work_items") is None


# --- inconclusive probes must not block, and must not be cached ------------


@pytest.mark.parametrize(
    "outcome",
    [
        HttpError("server error", 500, None),
        HttpError("unauthorized", 401, None),
        ConnectionError("network unreachable"),
    ],
    ids=["http-500", "http-401", "network"],
)
def test_inconclusive_probe_returns_none_and_does_not_block(outcome):
    """An unreachable probe is not evidence that filtering is broken."""
    client = make_client(outcome)
    assert probe_pql_support(client, WORKSPACE) is None
    assert guard_pql(client, WORKSPACE, 'assignee = "abc"', "list_work_items") is None


def test_inconclusive_probe_is_not_cached_so_it_can_be_retried():
    client = make_client(HttpError("server error", 500, None))
    probe_pql_support(client, WORKSPACE)
    assert cached_pql_support() is None

    # A later, healthy probe against the same deployment must still be able to
    # settle the question — a cached "unknown" would freeze it forever.
    healthy = make_client(HttpError("bad pql", 400, {"pql": "syntax error"}))
    assert probe_pql_support(healthy, WORKSPACE) is True


# --- caching ---------------------------------------------------------------


def test_verdict_is_cached_so_the_probe_runs_once_per_deployment():
    client = make_client(SimpleNamespace(results=[], total_count=1))
    probe_pql_support(client, WORKSPACE)
    probe_pql_support(client, WORKSPACE)
    probe_pql_support(client, WORKSPACE)
    assert len(client.work_items.calls) == 1


def test_separate_deployments_are_probed_separately():
    unsupported = make_client(SimpleNamespace(results=[], total_count=1), base_url="https://self-hosted.example")
    supported = make_client(HttpError("bad pql", 400, {"pql": "x"}), base_url="https://api.plane.so")

    assert probe_pql_support(unsupported, WORKSPACE) is False
    assert probe_pql_support(supported, WORKSPACE) is True
    # One deployment ignoring PQL is enough to stop advertising it as available.
    assert cached_pql_support() is False


# --- the guard must never probe when there is nothing to guard -------------


def test_no_pql_means_no_probe_and_no_refusal():
    client = make_client(SimpleNamespace(results=[], total_count=1))
    assert guard_pql(client, WORKSPACE, None, "list_work_items") is None
    assert guard_pql(client, WORKSPACE, "", "list_work_items") is None
    assert client.work_items.calls == []


def test_cached_support_is_unknown_before_any_probe():
    assert cached_pql_support() is None


# --- route selection: the workspace route is absent on some deployments ----


PROJECT = "24f16a05-86ea-4652-b294-500ec58d2abf"


def test_probe_prefers_the_project_route_when_a_project_is_known():
    client = make_client(SimpleNamespace(results=[], total_count=1))
    probe_pql_support(client, WORKSPACE, PROJECT)
    assert [c[0] for c in client.work_items.calls] == ["project"]


def test_probe_falls_back_to_the_workspace_route_when_the_project_route_is_absent():
    """A self-hosted deployment can 404 one route and serve the other."""
    client = make_client(
        SimpleNamespace(results=[], total_count=1),
        project_outcome=HttpError("no route", 404, None),
    )
    assert probe_pql_support(client, WORKSPACE, PROJECT) is False
    assert [c[0] for c in client.work_items.calls] == ["project", "workspace"]


def test_probe_is_inconclusive_when_both_routes_are_absent():
    """404 everywhere says nothing about PQL — it must not be read as a verdict."""
    client = make_client(HttpError("no route", 404, None))
    assert probe_pql_support(client, WORKSPACE, PROJECT) is None
    assert cached_pql_support() is None
    assert guard_pql(client, WORKSPACE, 'assignee = "abc"', "list_work_items", PROJECT) is None


def test_project_route_verdict_short_circuits_the_fallback():
    """A conclusive first probe must not fire a second request."""
    client = make_client(
        HttpError("should not be reached", 500, None),
        project_outcome=HttpError("bad pql", 400, {"pql": "syntax error"}),
    )
    assert probe_pql_support(client, WORKSPACE, PROJECT) is True
    assert [c[0] for c in client.work_items.calls] == ["project"]
