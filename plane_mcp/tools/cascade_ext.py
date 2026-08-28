"""Cascade-ext tools for Plane MCP Server (The1Studio fork feature).

Calls the fork's cascade-confirmation endpoints, which mirror a new terminal
status (completed/cancelled) down a whole descendant tree. Two subjects share
one `cascade_ext` app:

  Work items:
    GET  /api/cascade-ext/workspaces/<slug>/projects/<project_id>/issues/<issue_id>/
         cascade-preview/?group=<completed|cancelled>
    POST /api/cascade-ext/workspaces/<slug>/projects/<project_id>/issues/<issue_id>/
         cascade-apply/                                { state_id, child_ids? }
  Modules (plans/260828-module-cascade-terminal-status/):
    GET  /api/cascade-ext/workspaces/<slug>/projects/<project_id>/modules/<module_id>/
         cascade-preview/?status=<completed|cancelled>
    POST /api/cascade-ext/workspaces/<slug>/projects/<project_id>/modules/<module_id>/
         cascade-apply/                                { status, item_ids? }

Note the query parameter differs between subjects on purpose: the issue
preview takes `group`, the module preview takes `status` (a MODULE status, not
a state group) — the two are not unified server-side.

The official Plane SDK does not model any of these endpoints, so we issue raw
authenticated requests reusing the resolved client's base URL + auth — same
pattern as plane_mcp/tools/views_ext.py.

TRAP: same as views_ext.py — `client.config.base_path` ends in "/api/v1", but
cascade-ext mounts at "/api/cascade-ext/", OUTSIDE v1. The suffix is stripped
explicitly below; do not rely on httpx URL joining to reach it. This applies
to BOTH subjects — the module routes in `preview_module_cascade` and
`update_module(cascade=...)` inherit it exactly as the issue routes do.

`cascade-apply` moves the subject's own state/status AND cascades to its
descendants in one transaction (it does not expect the subject to have already
been PATCHed) — see plane.cascade_ext.service.apply_cascade (issues) and
apply_module_cascade (modules) in the fork. `update_work_item`'s `cascade=True`
path (plane_mcp/tools/work_items.py) and `update_module`'s `cascade=True` path
(plane_mcp/tools/modules.py) rely on this: each calls cascade-apply instead of
a plain state/status write once it determines the target is terminal.

Since 2026-08-28 (Phase 0 of the module-cascade plan) a work item already in a
terminal group PRUNES its entire subtree — nothing beneath it is listed,
walked, or changed, for either subject. A plain state/status PATCH never
cascades, from any client.

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

    @mcp.tool()
    def preview_module_cascade(
        workspace_slug: str,
        project_id: str,
        module_id: str,
        status: str,
    ) -> dict[str, Any]:
        """
        Preview what would cascade if `module_id` entered terminal `status`
        right now. Read-only — this changes nothing.

        Call this before `update_module(..., status=..., cascade=True)` to
        decide whether cascading is worth confirming with a user: an empty
        `items` list means every member plus descendant is already terminal
        (or there are none), so a plain `update_module(..., cascade=False)`
        status change is equivalent and there is nothing to cascade.

        The query parameter is `status` (a MODULE status), NOT the `group`
        the work-item preview takes — the two are deliberately not unified
        server-side.

        Requires the fork's `cascade_ext` app (The1Studio/plane branch
        `feat/module-cascade-terminal-status`) on the server — 404 against
        upstream Plane / Plane Cloud, or a server predating it.

        Args:
            workspace_slug: Workspace slug.
            project_id: UUID of the project.
            module_id: UUID of the module.
            status: Terminal MODULE status to preview entering — "completed"
                or "cancelled". Any other value returns 400.

        Returns:
            {target_group, depth_capped, over_cap, cap, summary, items}.
            `items` lists the currently-eligible members plus descendants a
            matching cascade-apply call would update, each with an `eligible`
            flag and a `reason` when disabled (no_matching_state /
            no_permission / already_terminal / under_terminal_ancestor /
            not_in_module_tree / not_eligible). `already_terminal` rows are
            never listed and everything beneath them is pruned. `over_cap`
            True (with an empty `items`) means the module exceeds
            MAX_MODULE_CASCADE_ITEMS (100) and cascade-apply would refuse
            with 400 rather than half-apply.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        return _send(
            client,
            "GET",
            f"/workspaces/{workspace_slug}/projects/{project_id}/modules/{module_id}/cascade-preview/",
            params={"status": status},
        )
