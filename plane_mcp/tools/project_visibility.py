"""Project visibility tools for Plane MCP Server (The1Studio fork feature).

WHY THIS EXISTS: `Project.network` (0 = secret/private, 2 = public) is a real
column on the core Plane model, but it is absent from the core public-API
serializer (`plane.api.serializers.project.ProjectCreateSerializer.Meta.fields`),
so DRF silently drops it on both create and update. A PATCH carrying
{"network": 0} returns 200 OK with the project still public — a
successful-looking no-op, which is why `update_project(network=...)` never
worked despite advertising the parameter.

The fork exposes the field from its own `project_ext` Django app instead of
editing core (docs/FORK.md). These tools call those endpoints:

    GET/PATCH /api/v1/workspaces/<slug>/projects/<project_id>/visibility/
    PATCH     /api/v1/workspaces/<slug>/project-visibility/

Requires the fork's API (plane.project_ext installed) — against upstream Plane
or Plane Cloud these tools return a 404.
"""

from typing import Any

from fastmcp import FastMCP

from plane_mcp.client import get_plane_client_context
from plane_mcp.tools.workload import _send

# Mirrors plane.db.models.project.Project.NETWORK_CHOICES.
NETWORK_SECRET = 0
NETWORK_PUBLIC = 2

_VISIBILITY_ALIASES = {
    "private": NETWORK_SECRET,
    "secret": NETWORK_SECRET,
    "public": NETWORK_PUBLIC,
}


def _coerce_network(visibility: str | int) -> int:
    """Accept 0/2 or the words private/secret/public. Raise on anything else.

    Deliberately strict: silently coercing an unrecognised value is the exact
    failure this module exists to fix.
    """
    if isinstance(visibility, bool):
        raise ValueError("visibility must be 'private'/'public' or 0/2, not a boolean")

    if isinstance(visibility, int):
        if visibility not in (NETWORK_SECRET, NETWORK_PUBLIC):
            raise ValueError(f"invalid visibility {visibility} — expected 0 (private) or 2 (public)")
        return visibility

    token = str(visibility).strip().lower()
    if token in _VISIBILITY_ALIASES:
        return _VISIBILITY_ALIASES[token]
    if token in ("0", "2"):
        return int(token)

    raise ValueError(f"invalid visibility {visibility!r} — expected 'private', 'public', 0 or 2")


def register_project_visibility_tools(mcp: FastMCP) -> None:
    """Register project visibility tools with the MCP server."""

    @mcp.tool()
    def get_project_visibility(project_id: str, workspace_slug: str | None = None) -> dict[str, Any]:
        """
        Get a project's visibility (private vs public).

        Args:
            project_id: UUID of the project

        Returns:
            {id, name, identifier, network, visibility} where network is
            0 (secret/private) or 2 (public) and visibility is the label.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        return _send(client, "GET", f"/workspaces/{workspace_slug}/projects/{project_id}/visibility/")

    @mcp.tool()
    def set_project_visibility(project_id: str, visibility: str, workspace_slug: str | None = None) -> dict[str, Any]:
        """
        Set a project's visibility to private or public.

        Use this instead of `update_project(network=...)` — the core /api/v1/
        project serializer does not include `network`, so update_project can
        never change visibility and will report success without doing anything.

        Requires workspace admin. Requires the The1Studio fork's `project_ext`
        app on the server (404 against upstream Plane / Plane Cloud).

        Args:
            project_id: UUID of the project
            visibility: "private" (or "secret") / "public". 0 and 2 also accepted.

        Returns:
            {id, name, identifier, network, visibility} after the change.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        network = _coerce_network(visibility)
        return _send(
            client,
            "PATCH",
            f"/workspaces/{workspace_slug}/projects/{project_id}/visibility/",
            json={"network": network},
        )

    @mcp.tool()
    def set_projects_visibility_bulk(
        project_ids: list[str], visibility: str, workspace_slug: str | None = None
    ) -> dict[str, Any]:
        """
        Set visibility on many projects in one request.

        Bulk companion to `set_project_visibility` — avoids an N+1 of per-project
        calls when flipping a whole workspace. Every id must belong to the active
        workspace; one unknown id fails the entire call rather than applying a
        partial update.

        Requires workspace admin, and the fork's `project_ext` app.

        Args:
            project_ids: List of project UUIDs.
            visibility: "private" (or "secret") / "public". 0 and 2 also accepted.

        Returns:
            {network, visibility, requested, updated, unchanged}.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        network = _coerce_network(visibility)
        return _send(
            client,
            "PATCH",
            f"/workspaces/{workspace_slug}/project-visibility/",
            json={"project_ids": project_ids, "network": network},
        )
