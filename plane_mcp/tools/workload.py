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
            project_id: Optional single project UUID — when set, queries the
                project-scoped workload route instead of the workspace route.

        Returns:
            Workload response: {granularity, date_from, date_to, periods[],
            rows[{assignee_id, assignee_name, buckets{period: hours}, total}],
            unscheduled[{assignee_id, hours}], meta{...}}.
        """
        client, workspace_slug = get_plane_client_context()

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
    def get_issue_workload_estimate(
        project_id: str,
        work_item_id: str,
    ) -> dict[str, Any]:
        """
        Get the time estimate (in hours) for a work item.

        Args:
            project_id: UUID of the project.
            work_item_id: UUID of the work item.

        Returns:
            The estimate object, or `{"hours": null}` when none is set.
        """
        client, workspace_slug = get_plane_client_context()
        path = f"/workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}/workload-estimate/"
        return _send(client, "GET", path)

    @mcp.tool()
    def set_issue_workload_estimate(
        project_id: str,
        work_item_id: str,
        hours: float,
    ) -> dict[str, Any]:
        """
        Set (upsert) the time estimate (in hours) for a work item.

        Args:
            project_id: UUID of the project.
            work_item_id: UUID of the work item.
            hours: Estimated hours (>= 0, <= 10000). Quantized to 2 decimals.

        Returns:
            The created/updated estimate object.
        """
        client, workspace_slug = get_plane_client_context()
        path = f"/workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}/workload-estimate/"
        return _send(client, "PUT", path, json={"hours": hours})

    @mcp.tool()
    def delete_issue_workload_estimate(
        project_id: str,
        work_item_id: str,
    ) -> None:
        """
        Delete the time estimate for a work item.

        Args:
            project_id: UUID of the project.
            work_item_id: UUID of the work item.
        """
        client, workspace_slug = get_plane_client_context()
        path = f"/workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}/workload-estimate/"
        _send(client, "DELETE", path)
