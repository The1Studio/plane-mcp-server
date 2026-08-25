"""
Runtime detection of PQL support on the connected Plane deployment.

Plane's hosted API implements PQL. Some self-hosted deployments do not, and
Django silently ignores query parameters it does not recognise — so a `pql=`
the server never reads comes back HTTP 200 carrying the **unfiltered** result
set and a `total_count` that reads as a legitimate filtered count. The caller
cannot distinguish a filtered answer from an unfiltered one, which makes every
count, audit, or bulk operation built on the filter quietly wrong (issue #32).

We settle the question once per base URL by sending a deliberately malformed
expression. A server that parses PQL must reject it; a server that ignores the
parameter answers 200. The verdict is cached so the cost is one extra request
per deployment per process.
"""

from typing import Any

from fastmcp.utilities.logging import get_logger
from plane import PlaneClient
from plane.errors import HttpError
from plane.models import WorkItemQueryParams

logger = get_logger(__name__)

# Deliberately unparseable: unbalanced parens, a bare operator, and an
# identifier no schema defines. Any real PQL parser rejects this outright.
PQL_PROBE_EXPRESSION = '__pql_support_probe__ ((( = "" AND'

# base_url -> supported?  Absent means "not yet probed".  A value is only
# written when the probe was conclusive.
_PQL_SUPPORT_CACHE: dict[str, bool] = {}


def _base_url(client: PlaneClient) -> str:
    return str(getattr(client.config, "base_path", "") or "")


def reset_pql_support_cache() -> None:
    """Clear the cached per-deployment verdicts (used by tests)."""
    _PQL_SUPPORT_CACHE.clear()


def _run_probe(client: PlaneClient, workspace_slug: str, project_id: str | None) -> bool | None:
    """Issue one probe request. See probe_pql_support for the return contract."""
    params = WorkItemQueryParams(pql=PQL_PROBE_EXPRESSION, per_page=1, fields="id")
    try:
        if project_id:
            client.work_items.list(workspace_slug=workspace_slug, project_id=project_id, params=params)
        else:
            client.work_items.list_workspace(workspace_slug=workspace_slug, params=params)
    except HttpError as e:
        if e.status_code == 400:
            # The server read the expression and refused it — PQL is live.
            return True
        # 404 in particular means the route itself is absent on this
        # deployment, which says nothing about PQL either way.
        logger.debug("PQL probe inconclusive: HTTP %s", e.status_code)
        return None
    except Exception as e:  # noqa: BLE001 - an unusable probe must not break the call
        logger.debug("PQL probe inconclusive: %s", e)
        return None
    # HTTP 200 for an expression no parser accepts: the parameter was never
    # looked at, and any filter we send is silently discarded.
    return False


def probe_pql_support(client: PlaneClient, workspace_slug: str, project_id: str | None = None) -> bool | None:
    """
    Determine whether this deployment actually evaluates `pql`.

    Returns True (rejected the malformed probe, so it parses PQL), False
    (accepted it, so it ignores the parameter), or None when the probe was
    inconclusive — a network error, an auth failure, a missing route, or any
    status other than the two we can interpret.

    `project_id` picks the project-scoped list route. Prefer passing it: some
    self-hosted deployments do not expose the workspace-wide route at all, and
    a probe that 404s can never settle the question.
    """
    cached = _PQL_SUPPORT_CACHE.get(_base_url(client))
    if cached is not None:
        return cached

    supported = _run_probe(client, workspace_slug, project_id)
    if supported is None and project_id:
        # The project route was unusable; the workspace route may still answer.
        supported = _run_probe(client, workspace_slug, None)
    if supported is None:
        return None

    _PQL_SUPPORT_CACHE[_base_url(client)] = supported
    if not supported:
        logger.warning(
            "Plane deployment at %s ignores the `pql` query parameter; "
            "PQL-filtered calls will be refused rather than answered with "
            "unfiltered rows.",
            _base_url(client) or "<unknown>",
        )
    return supported


def pql_unsupported_error(pql: str, tool: str) -> dict[str, Any]:
    """The structured refusal returned in place of unfiltered results."""
    return {
        "error": (
            "This Plane deployment does not implement PQL. It accepted the "
            "`pql` parameter with HTTP 200 but never evaluated it, so "
            "returning results here would mean handing you the UNFILTERED "
            "set as though it were filtered."
        ),
        "failed_pql": pql,
        "tool": tool,
        "hint": (
            "Re-run without `pql` and filter the results yourself, or use the "
            "endpoint's own typed arguments where it has them. Do not treat a "
            "`total_count` from a PQL-filtered call on this deployment as a "
            "filtered count."
        ),
        "pql_supported": False,
    }


def guard_pql(
    client: PlaneClient,
    workspace_slug: str,
    pql: str | None,
    tool: str,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Return a refusal payload when `pql` was supplied to a deployment that
    ignores it, or None when the call may proceed.

    Proceeds on an inconclusive probe: an unreachable probe is not evidence
    that filtering is broken, and blocking on it would take PQL away from
    deployments that support it.
    """
    if not pql:
        return None
    if probe_pql_support(client, workspace_slug, project_id) is False:
        return pql_unsupported_error(pql, tool)
    return None


def cached_pql_support() -> bool | None:
    """
    Report what probing has already established, without issuing a request.

    Returns False when some deployment reached this process has been observed
    ignoring `pql`, True when every probed deployment evaluated it, and None
    when nothing has been probed yet. Documentation-only callers use this so
    reading the reference never costs an HTTP round trip; a False here is the
    signal that the documented language is not implemented.
    """
    if not _PQL_SUPPORT_CACHE:
        return None
    return all(_PQL_SUPPORT_CACHE.values())
