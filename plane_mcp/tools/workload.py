"""Workload-related tools for Plane MCP Server (The1Studio fork feature).

These call the fork's public-API endpoints (/api/v1/.../workload/ and
.../workload-estimate/), which the official Plane SDK does not model — so we
issue raw authenticated requests reusing the resolved client's base URL + auth.
"""

from typing import Any

import httpx
from fastmcp import FastMCP

from plane_mcp.client import get_plane_client_context

_TIMEOUT = 30.0


def _send(
    client: Any,
    method: str,
    path: str,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Issue an authenticated request to a workload endpoint.

    `path` is relative to the SDK base path (which already includes /api/v1),
    e.g. "/workspaces/{slug}/workload/". Auth mirrors the SDK: X-Api-Key for
    API keys, Bearer for OAuth access tokens.
    """
    config = client.config
    url = f"{config.base_path.rstrip('/')}{path}"

    headers: dict[str, str] = {"Accept": "application/json"}
    if getattr(config, "api_key", None):
        headers["X-Api-Key"] = config.api_key
    elif getattr(config, "access_token", None):
        headers["Authorization"] = f"Bearer {config.access_token}"
    if json is not None:
        headers["Content-Type"] = "application/json"

    response = httpx.request(method, url, headers=headers, json=json, params=params, timeout=_TIMEOUT)
    if response.status_code >= 400:
        # Surface the server's error payload ({"error", "error_code"?}) instead
        # of httpx's bare status line — e.g. PUT on a parent issue returns
        # 400 {"error": "...", "error_code": "PARENT_HAS_CHILDREN"} and the
        # caller needs both to react meaningfully.
        detail = ""
        try:
            body = response.json()
            detail = body.get("error") or ""
            if body.get("error_code"):
                detail = f"{detail} [{body['error_code']}]".strip()
        except Exception:  # noqa: BLE001 — non-JSON error body; fall through
            pass
        if detail:
            raise httpx.HTTPStatusError(
                f"{response.status_code} {response.reason_phrase} for {url}: {detail}",
                request=response.request,
                response=response,
            )
    response.raise_for_status()
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def register_workload_tools(mcp: FastMCP) -> None:
    """Register all workload-related tools with the MCP server."""

    @mcp.tool()
    def get_workload(
        granularity: str,
        date_from: str,
        date_to: str,
        project_ids: list[str] | None = None,
        assignee_ids: list[str] | None = None,
        state_group: list[str] | None = None,
        project_id: str | None = None,
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """
        Get the per-person workload matrix (summed estimated hours per assignee,
        bucketed by day/week/month).

        Args:
            granularity: Bucket size — "day", "week", or "month".
            date_from: Window start, ISO date "YYYY-MM-DD".
            date_to: Window end, ISO date "YYYY-MM-DD".
                Max span: day <= 92, week <= 366, month <= 730 days.
            project_ids: Optional list of project UUIDs to scope to (intersected
                with the caller's accessible projects).
            assignee_ids: Optional list of assignee UUIDs to filter rows by.
            state_group: Optional list of state groups to include
                (backlog, unstarted, started, completed, cancelled). Default
                excludes completed + cancelled.

        Note:
            The matrix counts LEAF work items only — a parent (an item with
            sub-items) never appears as its own hours row; its hours live on
            its sub-items (prevents double-counting). Use
            `get_workload_rollups` to read a parent's derived totals.
            project_id: Optional single project UUID — when set, queries the
                project-scoped workload route instead of the workspace route.

        Returns:
            Workload response: {granularity, date_from, date_to, periods[],
            rows[{assignee_id, assignee_name, buckets{period: hours}, total}],
            unscheduled[{assignee_id, hours}], meta{...}}.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)

        params: dict[str, Any] = {
            "granularity": granularity,
            "date_from": date_from,
            "date_to": date_to,
        }
        if project_ids:
            params["project_ids"] = ",".join(project_ids)
        if assignee_ids:
            params["assignee_ids"] = ",".join(assignee_ids)
        if state_group:
            params["state_group"] = ",".join(state_group)

        if project_id:
            path = f"/workspaces/{workspace_slug}/projects/{project_id}/workload/"
        else:
            path = f"/workspaces/{workspace_slug}/workload/"

        return _send(client, "GET", path, params=params)

    @mcp.tool()
    def get_issue_workload_estimates_bulk(
        issue_ids: list[str],
        workspace_slug: str | None = None,
    ) -> dict[str, float]:
        """
        Get time estimates (in hours) for many work items in one request.

        This is the bulk companion to `get_issue_workload_estimate` — it avoids
        an N+1 of per-issue calls when you need the estimates for a list of
        work items (e.g. rendering a grid). The lookup is workspace-scoped and
        respects project membership + the guest "only my assigned issues" rule.

        Args:
            issue_ids: List of work-item UUIDs (max 500). Empty or all-invalid
                lists are rejected by the server with a 400.

        Returns:
            A mapping of work-item UUID -> estimated hours, e.g.
            `{"<uuid>": 3.5, "<uuid>": 0}`. Work items with no stored estimate
            (or outside the caller's accessible projects) are omitted; a stored
            value of `0` IS returned. PARENT work items (those with sub-items)
            are ALSO omitted — a parent looks like "no estimate" here; read its
            derived totals via `get_workload_rollups` instead.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        path = f"/workspaces/{workspace_slug}/workload-estimates/"
        return _send(client, "GET", path, params={"issue_ids": ",".join(issue_ids)})

    @mcp.tool()
    def get_workload_rollups(
        issue_ids: list[str],
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """
        Get derived (rolled-up) workload data for PARENT work items — items
        that have sub-items. Parents never carry their own estimate; their
        hours, due date and progress are computed from their sub-item tree.

        Rollup semantics: full-tree recursion (depth 10) over "countable"
        descendants (not deleted/archived/draft, state group not cancelled or
        triage). `hours` = sum of leaf estimates; `done_hours` = sum of leaf
        estimates in completed-group states; `percent` = done_hours / hours
        (0..1 fraction, null when hours is 0); `due_date` = max target_date
        over all countable descendants; `leaf_count` = leaves with an estimate.
        Guests with restricted visibility get scope-partial rollups by design.

        Args:
            issue_ids: List of work-item UUIDs (max 500). Empty or all-invalid
                lists are rejected by the server with a 400.

        Returns:
            A mapping of work-item UUID -> rollup object for the ids that ARE
            parents; non-parent (leaf) ids are omitted. Example:
            `{"<uuid>": {"hours": 10.0, "done_hours": 6.0, "percent": 0.6,
            "due_date": "2026-08-12", "leaf_count": 2}}`. An empty mapping
            means none of the requested items have sub-items.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        path = f"/workspaces/{workspace_slug}/workload-rollups/"
        return _send(client, "GET", path, params={"issue_ids": ",".join(issue_ids)})

    @mcp.tool()
    def get_issue_workload_estimate(
        project_id: str,
        work_item_id: str,
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """
        Get the time estimate (in hours) for a work item.

        Args:
            project_id: UUID of the project.
            work_item_id: UUID of the work item.

        Returns:
            The estimate object, or `{"hours": null}` when none is set.
            For a PARENT work item (one with sub-items) the response is
            `{"hours": null, "is_parent": true, "rollup": {hours, done_hours,
            percent, due_date, leaf_count}}` — parents never carry their own
            estimate; the rollup is derived from their sub-item tree.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        path = f"/workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}/workload-estimate/"
        return _send(client, "GET", path)

    @mcp.tool()
    def set_issue_workload_estimate(
        project_id: str,
        work_item_id: str,
        hours: float,
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """
        Set (upsert) the time estimate (in hours) for a work item.

        Args:
            project_id: UUID of the project.
            work_item_id: UUID of the work item.
            hours: Estimated hours (>= 0, <= 10000). Quantized to 2 decimals.

        Returns:
            The created/updated estimate object.

        Raises:
            400 with error_code PARENT_HAS_CHILDREN when the work item has
            sub-items — parents derive their estimate from the sub-item tree
            (see `get_workload_rollups`); set estimates on the sub-items
            instead.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        path = f"/workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}/workload-estimate/"
        return _send(client, "PUT", path, json={"hours": hours})

    @mcp.tool()
    def delete_issue_workload_estimate(
        project_id: str,
        work_item_id: str,
        workspace_slug: str | None = None,
    ) -> None:
        """
        Delete the time estimate for a work item.

        Args:
            project_id: UUID of the project.
            work_item_id: UUID of the work item.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        path = f"/workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}/workload-estimate/"
        _send(client, "DELETE", path)
