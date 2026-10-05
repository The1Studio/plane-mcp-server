"""Page-related tools for Plane MCP Server."""

from typing import Any

import httpx
from fastmcp import FastMCP
from plane.models.pages import CreatePage, Page
from plane.models.work_item_pages import CreateWorkItemPage, WorkItemPage

from plane_mcp.client import get_plane_client_context
from plane_mcp.tools.workload import _send

# Body of Plane's custom 404 handler (plane.app.views.error_404) for a path NO
# route matches. The fork's own page endpoints answer a missing project/page
# with a different {"error": ...} message, so this exact string identifies a
# server that does not have the endpoints at all.
_UNROUTED_404_BODY = {"error": "Page not found."}


def _project_pages_path(workspace_slug: str, project_id: str, page_id: str | None = None) -> str:
    base = f"/workspaces/{workspace_slug}/projects/{project_id}/pages/"
    return f"{base}{page_id}/" if page_id else base


def _project_pages_send(client: Any, method: str, path: str, feature: str, json: dict[str, Any] | None = None) -> Any:
    """Call a project-page endpoint of The1Studio's Plane fork (`page_ext`).

    Core Plane's public v1 API has no project-page route on this build, and the
    plane-sdk's `client.pages.*_project_page` methods target the same URL, so
    they cannot work either. Two failures need translating, because the bare
    `httpx` text hides both causes:

    - 404 with Plane's unrouted-path body -> the server lacks the fork endpoints
      (an empty result would misread as "this project has no pages").
    - any other error whose body is DRF's ``{"detail": ...}`` (a 403 says why)
      -> keep the exception type, put the reason in the message.
    """
    try:
        return _send(client, method, path, json=json)
    except httpx.HTTPStatusError as exc:
        response = exc.response
        if response is None:
            raise
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 404 and body == _UNROUTED_404_BODY:
            raise RuntimeError(
                f"{feature} requires the The1Studio Plane fork's `page_ext` API endpoints, which returned the "
                "unrouted-path 404 on this server. Either this deployment predates that fork feature, or it is "
                "upstream Plane / Plane Cloud rather than the fork."
            ) from exc
        detail = body.get("detail") if isinstance(body, dict) else None
        if detail and str(detail) not in str(exc):
            raise httpx.HTTPStatusError(f"{exc}: {detail}", request=exc.request, response=response) from exc
        raise


def register_page_tools(mcp: FastMCP) -> None:
    """Register all page-related tools with the MCP server."""

    @mcp.tool()
    def list_pages(
        project_id: str | None = None,
        params: dict[str, Any] | None = None,
        workspace_slug: str | None = None,
    ) -> list[Page] | list[dict[str, Any]]:
        """
        List pages.

        Lists a project's pages if project_id is given, otherwise workspace-level pages.

        A project's pages come from the The1Studio fork's `page_ext` endpoint
        (core Plane's v1 API has no project-page route, so the SDK call 404s):
        the caller must be an active member of the project — a workspace admin
        who is not in it is refused (403; add them with `add_project_members`).
        Another user's private page is never listed. No page bodies here; use
        `retrieve_page` for the content. Workspace-level listing is unchanged.

        Args:
            project_id: UUID of the project. Omit to list workspace pages.
            params: Optional query parameters (workspace pages only; the project
                endpoint returns every visible page, unpaginated)

        Returns:
            Workspace pages: Page objects. Project pages: dicts of {id, name,
            access, owned_by, owned_by_display_name, parent, is_locked,
            is_archived, archived_at, created_at, updated_at}.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        if project_id is not None:
            return _project_pages_send(
                client, "GET", _project_pages_path(workspace_slug, project_id), feature="list_pages"
            )
        response = client.pages.list_workspace_pages(workspace_slug=workspace_slug, params=params)
        return response.results

    @mcp.tool()
    def attach_page_to_work_item(
        project_id: str,
        work_item_id: str,
        page_id: str,
        workspace_slug: str | None = None,
    ) -> WorkItemPage:
        """
        Link a page to a work item.

        Args:
            project_id: UUID of the project
            work_item_id: UUID of the work item
            page_id: UUID of the page to link

        Returns:
            WorkItemPage link object
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        return client.work_items.pages.create(
            workspace_slug=workspace_slug,
            project_id=project_id,
            work_item_id=work_item_id,
            data=CreateWorkItemPage(page_id=page_id),
        )

    @mcp.tool()
    def list_work_item_pages(
        project_id: str,
        work_item_id: str,
        workspace_slug: str | None = None,
    ) -> list[WorkItemPage]:
        """
        List all pages linked to a work item.

        Args:
            project_id: UUID of the project
            work_item_id: UUID of the work item

        Returns:
            List of WorkItemPage link objects
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        response = client.work_items.pages.list(
            workspace_slug=workspace_slug,
            project_id=project_id,
            work_item_id=work_item_id,
        )
        return response.results

    @mcp.tool()
    def detach_page_from_work_item(
        project_id: str,
        work_item_id: str,
        work_item_page_id: str,
        workspace_slug: str | None = None,
    ) -> None:
        """
        Remove a page link from a work item.

        Args:
            project_id: UUID of the project
            work_item_id: UUID of the work item
            work_item_page_id: UUID of the work item page link (not the page ID)
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        client.work_items.pages.delete(
            workspace_slug=workspace_slug,
            project_id=project_id,
            work_item_id=work_item_id,
            work_item_page_id=work_item_page_id,
        )

    @mcp.tool()
    def retrieve_page(
        page_id: str,
        project_id: str | None = None,
        workspace_slug: str | None = None,
    ) -> Page | dict[str, Any]:
        """
        Retrieve a page by ID.

        Retrieves a project page if project_id is given, otherwise a workspace page.

        A project page comes from the The1Studio fork's `page_ext` endpoint and
        needs active project membership (another user's private page: 403).

        Args:
            page_id: UUID of the page
            project_id: UUID of the project. Omit for a workspace page.

        Returns:
            Workspace page: Page object. Project page: dict of {id, name, access,
            owned_by, owned_by_display_name, parent, is_locked, is_archived,
            archived_at, created_at, updated_at, description_html,
            description_stripped (plain text)}. Edit `description_html` and send
            it back with `update_page`.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)

        if project_id is not None:
            return _project_pages_send(
                client,
                "GET",
                _project_pages_path(workspace_slug, project_id, page_id),
                feature="retrieve_page",
            )
        return client.pages.retrieve_workspace_page(
            workspace_slug=workspace_slug,
            page_id=page_id,
        )

    @mcp.tool()
    def create_page(
        name: str,
        description_html: str,
        project_id: str | None = None,
        access: int | None = None,
        color: str | None = None,
        is_locked: bool | None = None,
        archived_at: str | None = None,
        view_props: dict[str, Any] | None = None,
        logo_props: dict[str, Any] | None = None,
        external_id: str | None = None,
        external_source: str | None = None,
        workspace_slug: str | None = None,
    ) -> Page:
        """
        Create a page.

        Creates a project page if project_id is given, otherwise a
        workspace-level page.

        Args:
            name: Page name
            description_html: Page content in HTML format
            project_id: UUID of the project. Omit to create a workspace page.
            access: Access level for the page (integer)
            color: Page color
            is_locked: Whether the page is locked
            archived_at: Archive timestamp (ISO 8601 format)
            view_props: View properties dictionary
            logo_props: Logo properties dictionary
            external_id: External system identifier
            external_source: External system source name

        Returns:
            Created Page object
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)

        data = CreatePage(
            name=name,
            description_html=description_html,
            access=access,
            color=color,
            is_locked=is_locked,
            archived_at=archived_at,
            view_props=view_props,
            logo_props=logo_props,
            external_id=external_id,
            external_source=external_source,
        )

        if project_id is not None:
            return client.pages.create_project_page(
                workspace_slug=workspace_slug,
                project_id=project_id,
                data=data,
            )
        return client.pages.create_workspace_page(
            workspace_slug=workspace_slug,
            data=data,
        )

    @mcp.tool()
    def update_page(
        project_id: str,
        page_id: str,
        name: str | None = None,
        description_html: str | None = None,
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """
        Update a project page's name and/or content (The1Studio fork `page_ext`).

        Read the page with `retrieve_page`, change its `description_html`, and
        send the WHOLE new html back (this replaces the content; it is not a
        patch). Send only the fields you want changed. The server keeps the
        page's collaborative-editor state consistent with the new html (it
        clears the stale editor binary so the live editor rebuilds it from the
        html on next open) and snapshots the previous content into the page's
        version history, so the edit can be undone from the Plane UI.

        Refused: a locked page (423) and an archived page (409); another user's
        private page (403); a caller who is not an active project member (403).
        Read-modify-write promptly: if someone has the page open in the browser
        at that moment, their editor can overwrite this change.

        Args:
            project_id: UUID of the project
            page_id: UUID of the page
            name: New page title
            description_html: New full page content as HTML (must be non-empty)

        Returns:
            The updated page dict (as `retrieve_page`) plus `changed` (false when
            the request matched what was already stored) and
            `description_binary_cleared`.
        """
        if name is None and description_html is None:
            raise ValueError("update_page needs name and/or description_html")

        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description_html is not None:
            body["description_html"] = description_html

        client, workspace_slug = get_plane_client_context(workspace_slug)
        return _project_pages_send(
            client,
            "PATCH",
            _project_pages_path(workspace_slug, project_id, page_id),
            feature="update_page",
            json=body,
        )
