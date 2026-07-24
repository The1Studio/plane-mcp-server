"""GitHub integration tools for Plane MCP Server (The1Studio fork feature,
`github_ext` Django app).

These call the fork's `/api/github/...` endpoints, which sit OUTSIDE the
`/api/v1/...` root the official Plane SDK models — so, like `workload.py`, we
issue raw authenticated requests reusing the resolved client's base URL/auth
rather than going through `client.<resource>.<verb>()`.

Two features:
  - Three-tier (instance -> workspace -> project) PR-lifecycle status-automation
    config (`get_github_state_config` / `set_github_state_config`).
  - Work-item <-> GitHub links (`list_work_item_github_links`). There is no
    dedicated link-list endpoint: the fork mirrors each `WorkItemGithubLink`
    into the core `IssueLink` table, so links are read through the EXISTING
    issue-links SDK call and filtered/parsed here.
"""

import re
from typing import Any

import httpx
from fastmcp import FastMCP
from plane.models.work_items import PaginatedWorkItemLinkResponse

from plane_mcp.client import get_plane_client_context

_TIMEOUT = 30.0

# Must match plane/github_ext/services/state_transition.py EVENT_KEYS exactly
# — the server rejects any other key with a 400, but validating here first
# gives the caller a specific error instead of a bare HTTP failure.
_EVENT_KEYS = ("pr_opened", "pr_ready_for_review", "pr_merged")

# Title format written by the fork's link-mirror
# (plane/github_ext/services/link_writer.py: f"GitHub {link_type}: {external_id}").
_GITHUB_LINK_TITLE_RE = re.compile(r"^GitHub (branch|pr|commit|issue): (.+)$")


def _send(
    client: Any,
    method: str,
    path: str,
    json: dict[str, Any] | None = None,
) -> Any:
    """Issue an authenticated request to a `/api/github/...` endpoint.

    `path` is relative to the API root (NOT the SDK's `/api/v1` base_path —
    github_ext mounts directly under `/api/`), e.g. "/github/config/". Auth
    mirrors the SDK: X-Api-Key for API keys, Bearer for OAuth access tokens.
    """
    config = client.config
    base = config.base_path.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = f"{base}{path}"

    headers: dict[str, str] = {"Accept": "application/json"}
    if getattr(config, "api_key", None):
        headers["X-Api-Key"] = config.api_key
    elif getattr(config, "access_token", None):
        headers["Authorization"] = f"Bearer {config.access_token}"
    if json is not None:
        headers["Content-Type"] = "application/json"

    response = httpx.request(method, url, headers=headers, json=json, timeout=_TIMEOUT)
    if response.status_code >= 400:
        # Surface the server's {"error": "..."} payload instead of httpx's bare
        # status line — e.g. a bad state name returns 400 {"error": "state
        # 'Foo' not found in project"} and that message is the useful part.
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


def _validate_rules(rules: dict[str, str]) -> None:
    """Client-side mirror of the server's `_validate_rules_shape` — fails fast
    with a specific message instead of a bare 400.
    """
    if not isinstance(rules, dict):
        raise ValueError("rules must be an object")
    for key, value in rules.items():
        if key not in _EVENT_KEYS:
            raise ValueError(f"invalid event key '{key}' — must be one of {_EVENT_KEYS}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"value for '{key}' must be a non-empty string (a Plane state name)")


def register_github_ext_tools(mcp: FastMCP) -> None:
    """Register all github_ext-related tools with the MCP server."""

    @mcp.tool()
    def get_github_state_config(
        project_id: str | None = None,
        instance: bool = False,
    ) -> dict[str, Any]:
        """
        Get the PR-lifecycle -> work-item-state automation rules.

        Rules resolve **most-specific-wins** across three tiers, each merged
        over the one below it: built-in defaults -> instance-wide "global" ->
        per-workspace -> per-project. This call ALWAYS returns the fully
        RESOLVED rules visible at the requested tier — never just that tier's
        stored override. In particular a project-tier call returns the
        effective rules the automation will actually use for that project
        (defaults + global + workspace + project all merged), not merely
        what's been overridden at the project level.

        Tier is picked by which args are set:
          - project_id given -> project tier (uses the current workspace from
            context; requires workspace membership).
          - instance=True (project_id omitted) -> instance-wide tier
            (requires instance-admin; this is the rarely-overridden default
            every workspace inherits from).
          - neither given (default) -> current workspace's tier (requires
            workspace membership).

        Args:
            project_id: UUID of the project. When set, returns that
                project's fully-resolved effective rules.
            instance: When True (and project_id is omitted), reads the
                instance-wide "global" default row instead of the current
                workspace's tier. Requires instance-admin.

        Returns:
            {"rules": {"pr_opened": "<state name>",
                       "pr_ready_for_review": "<state name>",
                       "pr_merged": "<state name>"}}
            Built-in defaults if nothing has ever been configured:
            {"pr_opened": "In Progress", "pr_ready_for_review": "In Review",
             "pr_merged": "Done"}.

        Raises:
            ValueError: project_id and instance=True given together
                (ambiguous — pick one tier).
        """
        if project_id and instance:
            raise ValueError("project_id and instance=True are mutually exclusive — pick one tier")

        client, workspace_slug = get_plane_client_context()

        if instance:
            path = "/github/config/"
        elif project_id:
            path = f"/github/{workspace_slug}/projects/{project_id}/config/"
        else:
            path = f"/github/{workspace_slug}/config/"

        return _send(client, "GET", path)

    @mcp.tool()
    def set_github_state_config(
        rules: dict[str, str],
        project_id: str | None = None,
        instance: bool = False,
    ) -> dict[str, Any]:
        """
        Set (upsert) the PR-lifecycle -> work-item-state automation rules at
        one tier. See `get_github_state_config` for the full three-tier
        precedence — this only writes the ONE tier you target; it does not
        need to (and should not) repeat rules already correct at a lower tier.

        Args:
            rules: Partial or full mapping of event -> Plane state NAME. Keys
                MUST be exactly one of "pr_opened", "pr_ready_for_review",
                "pr_merged" — no others are accepted. You only need to include
                the events you want to change; unspecified events keep
                resolving from the tier(s) below.
            project_id: UUID of the project to target the project tier.
                The project-tier write additionally validates that every
                state name in `rules` exists in that project (400 if not) —
                global/workspace writes are shape-only (state names aren't
                checked against any specific project there).
            instance: When True (and project_id is omitted), writes the
                instance-wide "global" default row instead of the current
                workspace's tier. Requires instance-admin.

        Returns:
            {"rules": {...}} — the tier's rules as stored after this write
            (NOT the fully-resolved rules — use `get_github_state_config` to
            see the merged result other tiers will see).

        Raises:
            ValueError: rules has an invalid shape (bad key, empty/non-string
                value), or project_id and instance=True given together.
        """
        if project_id and instance:
            raise ValueError("project_id and instance=True are mutually exclusive — pick one tier")
        _validate_rules(rules)

        client, workspace_slug = get_plane_client_context()

        if instance:
            path = "/github/config/"
        elif project_id:
            path = f"/github/{workspace_slug}/projects/{project_id}/config/"
        else:
            path = f"/github/{workspace_slug}/config/"

        return _send(client, "PUT", path, json={"rules": rules})

    @mcp.tool()
    def list_work_item_github_links(
        project_id: str,
        work_item_id: str,
    ) -> list[dict[str, Any]]:
        """
        List the GitHub branch/PR/commit references linked to a work item.

        There is no dedicated github-links endpoint: the fork mirrors each
        detected reference into the work item's regular Links panel (core
        IssueLink), so this reads via the SAME issue-links call as
        `list_work_item_links` and filters to github.com URLs, parsing
        link_type/external_id back out of the mirrored link's title.

        Args:
            project_id: UUID of the project.
            work_item_id: UUID of the work item.

        Returns:
            List of {"link_type", "url", "external_id", "link_id"} — one per
            matched GitHub link. link_type is one of "branch", "pr", "commit",
            "issue". A github.com link whose title wasn't written by the
            fork's mirror (e.g. pasted in manually) is still returned, with
            link_type and external_id set to None.
        """
        client, workspace_slug = get_plane_client_context()
        response: PaginatedWorkItemLinkResponse = client.work_items.links.list(
            workspace_slug=workspace_slug,
            project_id=project_id,
            work_item_id=work_item_id,
        )

        matches = []
        for link in response.results:
            url = link.url or ""
            if "github.com" not in url:
                continue
            title_match = _GITHUB_LINK_TITLE_RE.match(link.title or "")
            matches.append(
                {
                    "link_type": title_match.group(1) if title_match else None,
                    "url": link.url,
                    "external_id": title_match.group(2) if title_match else None,
                    "link_id": link.id,
                }
            )
        return matches
