"""Views-ext tools for Plane MCP Server (The1Studio fork feature).

These call the fork's grouped workspace-issues endpoint
(/api/views-ext/workspaces/<slug>/issues/), which the official Plane SDK does
not model — so we issue raw authenticated requests reusing the resolved
client's base URL + auth, same as plane_mcp/tools/workload.py.

TRAP: `client.config.base_path` ends in `/api/v1`, but this endpoint mounts at
`/api/views-ext/`, OUTSIDE v1. The suffix is stripped explicitly below; do not
rely on httpx URL joining to reach it.
"""

from typing import Any

import httpx
from fastmcp import FastMCP

from plane_mcp.client import get_plane_client_context

_TIMEOUT = 30.0

_V1_SUFFIX = "/api/v1"


def _send(
    client: Any,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Issue an authenticated request to a views-ext endpoint.

    `path` is relative to the API ROOT (NOT the SDK base path): the SDK's
    `config.base_path` ends in "/api/v1", while views-ext mounts at
    "/api/views-ext/". We strip the "/api/v1" suffix explicitly and then append
    "/api/views-ext{path}" — composing against base_path directly would yield
    "/api/v1/views-ext/..." and 404. Auth mirrors the SDK: X-Api-Key for API
    keys, Bearer for OAuth access tokens.
    """
    config = client.config
    base = config.base_path.rstrip("/")
    if base.endswith(_V1_SUFFIX):
        base = base[: -len(_V1_SUFFIX)]
    url = f"{base}/api/views-ext{path}"

    headers: dict[str, str] = {"Accept": "application/json"}
    if getattr(config, "api_key", None):
        headers["X-Api-Key"] = config.api_key
    elif getattr(config, "access_token", None):
        headers["Authorization"] = f"Bearer {config.access_token}"

    response = httpx.request(method, url, headers=headers, params=params, timeout=_TIMEOUT)
    if response.status_code >= 400:
        # Surface the server's error payload instead of httpx's bare status
        # line — e.g. an out-of-allowlist group_by returns
        # 400 {"error": "..."} and the caller needs that message to react.
        detail = ""
        try:
            body = response.json()
            detail = body.get("error") or ""
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


def register_views_ext_tools(mcp: FastMCP) -> None:
    """Register all views-ext-related tools with the MCP server."""

    @mcp.tool()
    def list_workspace_view_issues(
        search: str | None = None,
        group_by: str | None = None,
        sub_group_by: str | None = None,
        before: str | None = None,
        after: str | None = None,
        order_by: str | None = None,
        cursor: str | None = None,
        per_page: int | None = None,
    ) -> dict[str, Any]:
        """
        List work items across an entire workspace (cross-project), grouped and
        paginated — the fork's workspace Views endpoint.

        This powers the workspace Views tab's layout switcher (List / Board /
        Calendar / Spreadsheet / Timeline). It is a DIFFERENT endpoint from
        `search_work_items` (which hits /api/v1/.../work-items/search and
        returns a flat envelope) — this one returns a grouped/paginated
        envelope.

        Args:
            search: Server-side search over work-item name (icontains),
                whole-integer sequence_id, and project identifier (icontains) —
                the same matcher the Plane command palette uses, so "PROJ-123",
                "123", and "PROJ" all resolve as users expect. IMPORTANT: an
                empty or absent `search` returns EVERYTHING — it is never a
                hidden filter.
            group_by: Field to group results by. Server-side allowlist (a
                value outside it returns 400): state_id, state__group,
                priority, labels__id, assignees__id, cycle_id,
                issue_module__module_id, target_date, project_id, created_by.
            sub_group_by: Second-level grouping from the same allowlist.
                Requires `group_by` to be set, and the two must differ, else
                the server returns 400.
            before: Target-date window end, "YYYY-MM-DD" (a malformed date
                returns 400).
            after: Target-date window start, "YYYY-MM-DD".
            order_by: Sort field (e.g. "-created_at", "priority",
                "-target_date").
            cursor: Pagination cursor from a previous response's `next_cursor`
                / `prev_cursor`.
            per_page: Page size override.

        Returns:
            The raw response dict. Envelope keys: grouped_by, sub_grouped_by,
            total_count, next_cursor, prev_cursor, next_page_results,
            prev_page_results, count, total_pages, total_results, results.
            When `group_by` is set, `results` is a dict keyed per group, each
            value {"results": [...], "total_results": N}; without grouping it
            is a flat list of work items. There is no SDK model for this
            shape — consume the dict directly.
        """
        client, workspace_slug = get_plane_client_context()

        params: dict[str, Any] = {}
        if search:
            params["search"] = search
        if group_by:
            params["group_by"] = group_by
        if sub_group_by:
            params["sub_group_by"] = sub_group_by
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if order_by:
            params["order_by"] = order_by
        if cursor:
            params["cursor"] = cursor
        if per_page is not None:
            params["per_page"] = per_page

        path = f"/workspaces/{workspace_slug}/issues/"
        return _send(client, "GET", path, params=params)
