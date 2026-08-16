"""Workspace-related tools for Plane MCP Server."""

from fastmcp import FastMCP
from plane.models.projects import ProjectFeature
from plane.models.users import UserLite
from plane.models.workspaces import WorkspaceFeature

from plane_mcp.client import (
    get_active_workspace,
    get_configured_workspace_slugs,
    get_plane_client_context,
    set_active_workspace,
)


def register_workspace_tools(mcp: FastMCP) -> None:
    """Register all workspace-related tools with the MCP server."""

    @mcp.tool()
    def list_workspaces() -> dict:
        """
        List the workspaces this server is configured to address, and report
        which of them the current credentials can actually reach.

        NOTE ON DISCOVERY: Plane's public API exposes no workspace-listing
        endpoint, so this cannot enumerate every workspace your account belongs
        to -- it reports the set declared in PLANE_WORKSPACE_SLUG /
        PLANE_WORKSPACE_SLUGS, each probed live. A workspace missing from the
        result is missing from the server's configuration, not necessarily from
        your account.

        Returns:
            dict with `active` (session default), `default` (env default) and
            `workspaces`: one entry per configured slug carrying `slug`,
            `reachable`, and either `project_count` or `error`.
        """
        slugs = get_configured_workspace_slugs()
        entries: list[dict] = []

        for slug in slugs:
            client, _ = get_plane_client_context(workspace_slug=slug)
            try:
                projects = client.projects.list(workspace_slug=slug)
                count = len(getattr(projects, "results", projects) or [])
                entries.append({"slug": slug, "reachable": True, "project_count": count})
            except Exception as exc:  # noqa: BLE001 - surfaced per-slug, never fatal
                entries.append({"slug": slug, "reachable": False, "error": str(exc)})

        return {
            "active": get_active_workspace(),
            "default": slugs[0] if slugs else None,
            "configured_count": len(slugs),
            "workspaces": entries,
        }

    @mcp.tool()
    def set_workspace(workspace_slug: str | None = None) -> dict:
        """
        Set the session-default workspace for subsequent tool calls.

        Every workspace-aware tool also accepts a per-call `workspace_slug`
        argument; use this when a whole run targets one workspace and you do not
        want to repeat it on each call.

        Args:
            workspace_slug: Slug to make the session default. Pass None to clear
                the override and fall back to the configured default.

        Returns:
            dict with the resulting `active` slug and whether it is `reachable`.
        """
        set_active_workspace(workspace_slug)

        if workspace_slug is None:
            return {"active": None, "reachable": None, "note": "cleared; using configured default"}

        client, slug = get_plane_client_context(workspace_slug)
        try:
            client.projects.list(workspace_slug=slug)
            return {"active": slug, "reachable": True}
        except Exception as exc:  # noqa: BLE001 - report, do not raise
            return {"active": slug, "reachable": False, "error": str(exc)}

    @mcp.tool()
    def get_workspace_members(workspace_slug: str | None = None) -> list[UserLite]:
        """
        Get all members of the current workspace.

        Returns:
            List of UserLite objects representing workspace members
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        return client.workspaces.get_members(workspace_slug=workspace_slug)

    @mcp.tool()
    def get_features(
        project_id: str | None = None, workspace_slug: str | None = None
    ) -> WorkspaceFeature | ProjectFeature:
        """
        Get feature flags.

        Returns a project's features if project_id is given, otherwise the
        workspace's features.

        Args:
            project_id: UUID of the project. Omit for workspace features.

        Returns:
            ProjectFeature when project_id is given, otherwise WorkspaceFeature.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        if project_id is not None:
            return client.projects.get_features(workspace_slug=workspace_slug, project_id=project_id)
        return client.workspaces.get_features(workspace_slug=workspace_slug)

    @mcp.tool()
    def update_workspace_features(
        project_grouping: bool | None = None,
        initiatives: bool | None = None,
        teams: bool | None = None,
        customers: bool | None = None,
        wiki: bool | None = None,
        pi: bool | None = None,
        workspace_slug: str | None = None,
    ) -> WorkspaceFeature:
        """
        Update features of the current workspace.

        Args:
            project_grouping: Enable/disable project grouping feature
            initiatives: Enable/disable initiatives feature
            teams: Enable/disable teams feature
            customers: Enable/disable customers feature
            wiki: Enable/disable wiki feature
            pi: Enable/disable PI (Program Increment) feature

        Returns:
            Updated WorkspaceFeature object
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)

        # Build data dict with only non-None values
        feature_data: dict[str, bool] = {}
        if project_grouping is not None:
            feature_data["project_grouping"] = project_grouping
        if initiatives is not None:
            feature_data["initiatives"] = initiatives
        if teams is not None:
            feature_data["teams"] = teams
        if customers is not None:
            feature_data["customers"] = customers
        if wiki is not None:
            feature_data["wiki"] = wiki
        if pi is not None:
            feature_data["pi"] = pi

        data = WorkspaceFeature(**feature_data)

        return client.workspaces.update_features(workspace_slug=workspace_slug, data=data)
