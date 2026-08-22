"""Cascade-ext tools for Plane MCP Server (The1Studio fork feature).

Calls the fork's cascade-confirmation endpoints, which mirror a work item's
new terminal state (completed/cancelled) down its whole descendant tree. The
official Plane SDK does not model either endpoint, so we issue raw
authenticated requests reusing the resolved client's base URL + auth — same
pattern as plane_mcp/tools/views_ext.py.

    GET  /api/cascade-ext/workspaces/<slug>/projects/<project_id>/issues/<issue_id>/
         cascade-preview/?group=<completed|cancelled>
    POST /api/cascade-ext/workspaces/<slug>/projects/<project_id>/issues/<issue_id>/
         cascade-apply/

TRAP: same as views_ext.py — `client.config.base_path` ends in "/api/v1", but
cascade-ext mounts at "/api/cascade-ext/", OUTSIDE v1. The suffix is stripped
explicitly below; do not rely on httpx URL joining to reach it.

`cascade-apply` moves the parent's own state AND cascades to descendants in
one transaction (it does not expect the parent to have already been PATCHed) —
see plane.cascade_ext.service.apply_cascade in the fork. `update_work_item`'s
`cascade=True` path (plane_mcp/tools/work_items.py) relies on this: it calls
cascade-apply instead of a plain state PATCH once it determines the target
state is terminal.

Depends on The1Studio/plane#54 (cascade_ext Django app, branch
feat/cascade-confirm-sub-items, not yet merged at the time this was written)
— 404s against a server that predates it, or against upstream Plane / Plane
Cloud.
"""

from typing import Any

import httpx
from fastmcp import FastMCP

from plane_mcp.client import get_plane_client_context

_TIMEOUT = 30.0
_V1_SUFFIX = "/api/v1"

# Mirrors plane.cascade_ext.views._VALID_TARGET_GROUPS in the fork.
TERMINAL_GROUPS = frozenset({"completed", "cancelled"})


def _send(
    client: Any,
    method: str,
    path: str,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Issue an authenticated request to a cascade-ext endpoint.

    `path` is relative to the API ROOT (NOT the SDK base path): the SDK's
    `config.base_path` ends in "/api/v1", while cascade-ext mounts at
    "/api/cascade-ext/". We strip the "/api/v1" suffix explicitly and then
    append "/api/cascade-ext{path}" — composing against base_path directly
    would yield "/api/v1/cascade-ext/..." and 404. Auth mirrors the SDK:
    X-Api-Key for API keys, Bearer for OAuth access tokens.
    """
    config = client.config
    base = config.base_path.rstrip("/")
    if base.endswith(_V1_SUFFIX):
        base = base[: -len(_V1_SUFFIX)]
    url = f"{base}/api/cascade-ext{path}"

    headers: dict[str, str] = {"Accept": "application/json"}
    if getattr(config, "api_key", None):
        headers["X-Api-Key"] = config.api_key
    elif getattr(config, "access_token", None):
        headers["Authorization"] = f"Bearer {config.access_token}"
    if json is not None:
        headers["Content-Type"] = "application/json"

    response = httpx.request(method, url, headers=headers, json=json, params=params, timeout=_TIMEOUT)
    if response.status_code >= 400:
        # Surface the server's error payload instead of httpx's bare status
        # line — e.g. an unrecognised `group` returns 400 {"error": "..."}
        # and the caller needs that message to react.
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


def register_cascade_ext_tools(mcp: FastMCP) -> None:
    """Register cascade-ext tools with the MCP server."""

    @mcp.tool()
    def preview_work_item_cascade(
        project_id: str,
        work_item_id: str,
        group: str,
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """
        Preview what would cascade if `work_item_id` entered terminal `group`
        right now. Read-only — this changes nothing.

        Call this before `update_work_item(..., state=..., cascade=True)` to
        decide whether cascading is worth confirming with a user: an empty
        `descendants` list means every descendant is already terminal (or
        there are none), so a plain `update_work_item(..., cascade=False)`
        state change is equivalent and there is nothing to cascade.

        Requires the fork's `cascade_ext` app on the server (404 against
        upstream Plane / Plane Cloud, or a server predating
        The1Studio/plane#54).

        Args:
            project_id: UUID of the project.
            work_item_id: UUID of the work item (the prospective parent).
            group: Terminal group to preview entering — "completed" or
                "cancelled". Any other value returns 400.
            workspace_slug: Workspace slug override.

        Returns:
            {target_group, depth_capped, descendants}. `depth_capped` is
            True when the server's descendant-depth limit truncated the
            tree; `descendants` lists the currently-eligible descendants a
            matching cascade-apply call would update (already-terminal
            descendants are excluded).
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        return _send(
            client,
            "GET",
            f"/workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}/cascade-preview/",
            params={"group": group},
        )
