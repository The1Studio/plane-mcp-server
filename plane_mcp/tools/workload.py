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
            project_id: Optional single project UUID — when set, queries the
                project-scoped workload route instead of the workspace route.
            assignee_ids: Optional list of assignee UUIDs to filter rows by.
            state_group: Optional list of state groups to include
                (backlog, unstarted, started, completed, cancelled). No
                default filter — when omitted, every state group is
                returned, including completed and cancelled.

        Scope:
            When `project_id` is omitted this queries the WORKSPACE route,
            which sums every project the caller may read — for a WORKSPACE
            ADMIN that is all projects in the workspace; for everyone else it
            is only the projects they are an active member of. `project_ids`
            is intersected with that set (never trusted outright), and
            `project_id` narrows to that one project. So a workspace-wide
            total and an explicit all-projects total agree for an admin, but
            for a non-admin they agree only when the explicit list is a
            subset of that caller's memberships. A GUEST in a project with
            `guest_view_all_features` off sees only their own assigned items,
            so their totals are scope-partial by design.

        Note:
            The matrix counts LEAF work items only — a parent (an item with
            sub-items) never appears as its own hours row; its hours live on
            its sub-items (prevents double-counting). Use
            `get_workload_rollups` to read a parent's derived totals.

        Returns:
            Workload response: {granularity, date_from, date_to, periods[]
            (spans the whole requested window, not just the periods that
            received hours — capacity_buckets/total_over below are priced
            against every period in this list), rows[...],
            unscheduled[{assignee_id, hours}], meta{...}}.

            meta carries issues_counted, issues_unscheduled,
            issues_unestimated, dirty_date_count, zero_estimate_count,
            unscheduled_ratio and truncated. issues_counted and
            issues_unscheduled describe HOURS, so they count estimated items
            only; issues_unestimated counts the rest, and is a superset of
            zero_estimate_count (which sees only stored rows with
            hours <= 0, not items with no row at all).

            Each entry in rows[] is {assignee_id (null = the Unassigned
            row), assignee_name, buckets{period: hours} (sparse),
            month_buckets{"YYYY-MM": hours} (sparse calendar-month totals,
            independent of `granularity`), total, capacity_buckets
            {period: hours}, over{period: bool}, total_over (bool — `total`
            exceeds the summed capacity_buckets across the WHOLE window),
            tasks[] (per-issue detail, capped at 200/assignee),
            tasks_truncated (bool — true when that cap was hit)}.

            Each entry in tasks[] is {id, project_id, identifier, name,
            hours (this assignee's share of the issue's estimate),
            total_hours (the issue's undivided estimate), assignee_count,
            start_date, target_date, state_group, state_name, state_color,
            unestimated, overdue}.

            unestimated is True when the work item has NO estimate row, or
            one with hours <= 0. Such an item carries hours: 0 and
            total_hours: 0, and contributes to NO capacity figure at all —
            buckets, month_buckets, capacity_buckets, over, total_over and
            the top-level unscheduled[] are identical to a response without
            it. It is a task row and nothing else.

            DO NOT infer this from hours == 0. A stored zero-hour estimate is
            a real, reachable state (the server reports those separately in
            meta.zero_estimate_count), so the arithmetic test misclassifies
            it. The flag is always present and never null; an estimated row
            carries False explicitly.

            Two consequences worth planning around: tasks[] is sorted with
            unestimated rows FIRST, and the 200/assignee cap is SHARED
            between the two kinds, so an assignee with a large unestimated
            backlog can have estimated rows truncated away (tasks_truncated
            reports it). Do not assume tasks[0] is the earliest-dated row.

            state_color is the state's own colour and is a FREE-FORM CSS
            colour string, not a guaranteed hex: server-side it is an
            unvalidated CharField, so "", "#fa0", "rgb(...)" and named
            colours are all reachable. Do not parse it, and do not assume it
            is non-empty — fall back to the state group's colour when it is
            blank. state_name is likewise normalised to "" rather than null.

            rows[] is ordered Unassigned first, then ascending by
            assignee_name (case-insensitive) — rows[0] is NOT the busiest
            assignee.

            rows[] COUNTS PEOPLE, NOT WORK. Every active, non-bot member of
            the in-scope projects gets a row whether or not they carry any
            estimated work, so `len(rows)` is a headcount and answers nothing
            about whether this window holds anything. A workspace with 30
            members and nothing scheduled returns 30 rows. To ask "is there
            work here", test the rows themselves:

                any(r["tasks"] or r["total"] for r in resp["rows"])

            Both halves are needed: `total` alone misses a member whose only
            work is unscheduled (those hours go to the top-level
            `unscheduled[]` bucket, never into `buckets`), and `tasks` alone
            misses hours whose task rows were cut by the 200/assignee cap.

            An empty row is not a gap in the data — it carries total: 0,
            tasks: [], and a FULLY POPULATED capacity_buckets. The unused
            capacity is the point of the row: it is how the response answers
            "who is free" as well as "who is overloaded".

            A member with no assigned work item and one whose work items are
            all unestimated are NO LONGER indistinguishable: since the server
            started returning unestimated items, the latter has a non-empty
            tasks[] whose rows all carry unestimated: True, while the former
            has tasks: []. Both still report total: 0, so a check written
            against `total` alone cannot tell them apart. meta
            .issues_unestimated gives the workspace-wide count.

            Membership is ProjectMember, never WorkspaceMember — someone with
            no in-scope project could never be assigned work this request
            returns, so they get no row. A flag-off guest
            (guest_view_all_features=False) sees only their OWN row in a
            restricted project: that project's member roster is exactly what
            the flag withholds. `assignee_ids` narrows empty rows too, so
            filtering to one member returns one row.

            This is unconditional — there is no parameter to switch empty
            rows off.
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

    @mcp.tool()
    def get_work_settings(workspace_slug: str | None = None) -> dict[str, Any]:
        """
        Get the workspace-wide workload settings (max daily hours cap, workdays,
        and week-start day) that `get_workload`'s capacity/overload columns are
        computed against.

        Callable by any workspace ADMIN or MEMBER (not GUEST).

        Returns:
            `{max_daily_hours: float, workdays: list[int] (0=Sun..6=Sat, ascending,
            never empty), week_start_day: int (0=Sun..6=Sat)}`. Always returns a
            value — a workspace with no settings row yet gets the server default
            (`max_daily_hours=8.0, workdays=[1,2,3,4,5]` i.e. Mon-Fri,
            `week_start_day=1` i.e. Monday); callers never need to branch on
            "not configured".
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        path = f"/workspaces/{workspace_slug}/work-settings/"
        return _send(client, "GET", path)

    @mcp.tool()
    def set_work_settings(
        max_daily_hours: float,
        workdays: list[int],
        week_start_day: int,
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """
        Set (upsert) the workspace-wide workload settings.

        Callable by a workspace ADMIN only.

        Args:
            max_daily_hours: Per-day hour cap (>= 0, <= 10000). Quantized to 2
                decimals. This is a PER-DAY cap, not per-week — there is no
                weekly-hours field; a workspace's effective weekly capacity is
                `max_daily_hours * len(workdays)`.
            workdays: Which days count as working days, Plane's weekday
                encoding (0=Sun..6=Sat). Must be non-empty — an empty list is
                rejected with a 400 (it would make weekly-capacity division by
                zero). Duplicates are rejected; the server normalizes the
                stored value to ascending order regardless of input order.
            week_start_day: First day of the week for bucketing, same 0=Sun..6=Sat
                encoding (0..6).

        Returns:
            The updated settings object, same shape as `get_work_settings`.

        Raises:
            A 400 response (surfaced as `httpx.HTTPStatusError` with the
            server's error detail) for an empty `workdays`, an out-of-range
            `week_start_day`/`workdays` entry, or `max_daily_hours` outside
            `[0, 10000]`.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        path = f"/workspaces/{workspace_slug}/work-settings/"
        return _send(
            client,
            "PUT",
            path,
            json={
                "max_daily_hours": max_daily_hours,
                "workdays": workdays,
                "week_start_day": week_start_day,
            },
        )
