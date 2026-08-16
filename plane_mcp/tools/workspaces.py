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
    def list_workspaces(candidates: list[str] | None = None) -> dict:
        """
        Report which workspaces this server can actually reach, probing both the
        configured slugs and any candidate slugs you supply.

        NOTE ON DISCOVERY: Plane's public API exposes no workspace-listing
        endpoint -- /api/v1/workspaces/ does not exist, and the web app's
        /api/users/me/workspaces/ rejects API keys (it needs a browser session).
        So this CANNOT enumerate the workspaces your account belongs to. It
        probes a set of slugs you name. A workspace missing from the result is
        missing from that set, not necessarily from your account.

        FINDING YOUR SLUG: it is the first path segment when you are logged into
        Plane -- <base-url>/<slug>/projects/. Read it from the browser address
        bar, then pass it here as a candidate to confirm before committing it to
        config.

        UNREACHABLE IS AMBIGUOUS: Plane answers a wrong slug and a real slug you
        lack access to with the same 403, so `reachable: false` means "this slug
        did not work", never "this workspace does not exist".

        Args:
            candidates: Extra slugs to probe alongside the configured ones. Use
                this to test a slug without restarting the server.

        Returns:
            dict with `active` (session default), `default` (env default),
            `workspaces` (one entry per probed slug carrying `slug`, `source`,
            `reachable`, and either `project_count` or `error`), and `note` when
            there was nothing to probe.
        """
        configured = get_configured_workspace_slugs()
        extra = [s.strip() for s in (candidates or []) if s and s.strip()]

        probed: list[tuple[str, str]] = [(s, "configured") for s in configured]
        seen = set(configured)
        for slug in extra:
            if slug not in seen:
                seen.add(slug)
                probed.append((slug, "candidate"))

        entries: list[dict] = []
        for slug, source in probed:
            client, _ = get_plane_client_context(workspace_slug=slug)
            try:
                projects = client.projects.list(workspace_slug=slug)
                count = len(getattr(projects, "results", projects) or [])
                entries.append({"slug": slug, "source": source, "reachable": True, "project_count": count})
            except Exception as exc:  # noqa: BLE001 - surfaced per-slug, never fatal
                entries.append({"slug": slug, "source": source, "reachable": False, "error": str(exc)})

        result = {
            "active": get_active_workspace(),
            "default": configured[0] if configured else None,
            "configured_count": len(configured),
            "probed_count": len(probed),
            "workspaces": entries,
        }

        # An empty result must not read like "you belong to no workspaces" --
        # say plainly that nothing was probed and how to fix it.
        if not probed:
            result["note"] = (
                "Nothing to probe: no workspace configured and no candidates given. "
                "Plane cannot enumerate workspaces, so pass candidates=['<slug>'] "
                "with the first path segment of your Plane URL "
                "(<base-url>/<slug>/projects/), or set PLANE_WORKSPACE_SLUG."
            )
        elif not any(e["reachable"] for e in entries):
            result["note"] = (
                "No probed slug was reachable. Plane returns the same 403 for a "
                "wrong slug and for a workspace you lack access to, so check the "
                "spelling against your Plane URL and confirm the API key belongs "
                "to an account in that workspace."
            )

        return result

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
