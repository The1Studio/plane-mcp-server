"""Module-related tools for Plane MCP Server."""

from typing import Annotated, Any, get_args

from fastmcp import FastMCP
from fastmcp.utilities.logging import get_logger
from plane.errors.errors import HttpError
from plane.models.enums import ModuleStatusEnum
from plane.models.modules import (
    CreateModule,
    Module,
    PaginatedArchivedModuleResponse,
    PaginatedModuleResponse,
    PaginatedModuleWorkItemResponse,
    UpdateModule,
)
from plane.models.query_params import WorkItemQueryParams
from pydantic import Field

from plane_mcp.client import get_plane_client_context
from plane_mcp.pql_support import guard_pql
from plane_mcp.tools.cascade_ext import TERMINAL_GROUPS
from plane_mcp.tools.cascade_ext import _send as _cascade_send
from plane_mcp.tools.pql_reference import PQL_FIELD_HINT, PQL_FULL_REFERENCE

logger = get_logger(__name__)


def register_module_tools(mcp: FastMCP) -> None:
    """Register all module-related tools with the MCP server."""

    @mcp.tool()
    def list_modules(
        project_id: str,
        archived: bool = False,
        params: dict[str, Any] | None = None,
        workspace_slug: str | None = None,
    ) -> list[Module]:
        """
        List modules in a project.

        Args:
            project_id: UUID of the project
            archived: Set True to list archived modules instead of active ones.
            params: Optional query parameters as a dictionary

        Returns:
            List of Module objects
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        if archived:
            archived_response: PaginatedArchivedModuleResponse = client.modules.list_archived(
                workspace_slug=workspace_slug, project_id=project_id, params=params
            )
            return archived_response.results
        response: PaginatedModuleResponse = client.modules.list(
            workspace_slug=workspace_slug, project_id=project_id, params=params
        )
        return response.results

    @mcp.tool()
    def create_module(
        project_id: str,
        name: str,
        description: str | None = None,
        start_date: str | None = None,
        target_date: str | None = None,
        status: str | None = None,
        lead: str | None = None,
        members: list[str] | None = None,
        external_source: str | None = None,
        external_id: str | None = None,
        workspace_slug: str | None = None,
    ) -> Module:
        """
        Create a new module.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            name: Module name
            description: Module description
            start_date: Module start date (ISO 8601 format)
            target_date: Module target/end date (ISO 8601 format)
            status: Module status (backlog, planned, in-progress, paused, completed, cancelled)
            lead: UUID of the user who leads the module
            members: List of user IDs who are members of the module
            external_source: External system source name
            external_id: External system identifier

        Returns:
            Created Module object
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)

        # Validate status against allowed literal values
        validated_status: ModuleStatusEnum | None = (
            status if status in get_args(ModuleStatusEnum) else None  # type: ignore[assignment]
        )

        data = CreateModule(
            name=name,
            description=description,
            start_date=start_date,
            target_date=target_date,
            status=validated_status,
            lead=lead,
            members=members,
            external_source=external_source,
            external_id=external_id,
        )

        return client.modules.create(workspace_slug=workspace_slug, project_id=project_id, data=data)

    @mcp.tool()
    def retrieve_module(project_id: str, module_id: str, workspace_slug: str | None = None) -> Module:
        """
        Retrieve a module by ID.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            module_id: UUID of the module

        Returns:
            Module object
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        return client.modules.retrieve(workspace_slug=workspace_slug, project_id=project_id, module_id=module_id)

    @mcp.tool()
    def update_module(
        project_id: str,
        module_id: str,
        name: str | None = None,
        description: str | None = None,
        start_date: str | None = None,
        target_date: str | None = None,
        status: str | None = None,
        lead: str | None = None,
        members: list[str] | None = None,
        external_source: str | None = None,
        external_id: str | None = None,
        cascade: bool = False,
        workspace_slug: str | None = None,
    ) -> Module | dict[str, Any]:
        """
        Update a module by ID.

        A plain update_module NEVER cascades, from any client: without
        `cascade=True`, a status change to "completed"/"cancelled" just
        updates the module itself — work items in it are left untouched,
        exactly like the web UI's "only change this module" path.

        When `cascade=True` AND `status` is a terminal value ("completed" or
        "cancelled"), the module's new status and every currently-eligible
        member plus descendant (the members' full sub-trees, recursive) are
        applied together via the fork's module cascade-apply endpoint
        (The1Studio/plane branch `feat/module-cascade-terminal-status`)
        instead of a plain status PATCH. When `cascade=True` but `status` is
        not terminal (backlog/planned/in-progress/paused, or any unrecognized
        value this tool coerces to None), this is also a plain PATCH — the
        flag never raises, so a caller may set it once and reuse it across
        calls. Use `preview_module_cascade` first to see what would cascade.

        The cascade is capped: more than MAX_MODULE_CASCADE_ITEMS (100) live
        items is a 400 REFUSAL, NOT a partial apply — the server writes
        nothing, module status included. A headless caller has no modal to
        read the refusal from; the raised error names the cap, so treat it
        as a decision point, not a transport failure to retry.

        Requires the fork's `cascade_ext` app on the server.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            module_id: UUID of the module
            name: Module name
            description: Module description
            start_date: Module start date (ISO 8601 format)
            target_date: Module target/end date (ISO 8601 format)
            status: Module status (backlog, planned, in-progress, paused, completed, cancelled)
            lead: UUID of the user who leads the module
            members: List of user IDs who are members of the module
            external_source: External system source name
            external_id: External system identifier
            cascade: Default False — the plain PATCH path, byte-for-byte what
                this tool did before `cascade` existed. When True AND the
                validated status is terminal, cascade-apply is called instead
                (see above).

        Returns:
            Updated Module object — unless the cascade path fires, in which
            case the cascade-apply response is returned instead: {"module",
            "status", "updated": ["<uuid>", ...], "rejected": [{"id",
            "reason"}, ...]}. Call retrieve_module afterward for the full
            module object.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)

        # Validate status against allowed literal values
        validated_status: ModuleStatusEnum | None = (
            status if status in get_args(ModuleStatusEnum) else None  # type: ignore[assignment]
        )

        # The cascade branch keys off the VALIDATED status, never the raw
        # argument: `status="Completed"` coerces to None above, so a cascade
        # keyed on the raw string would fire for a status the patch is NOT
        # setting. Terminality is a property of the module status literal
        # ("completed"/"cancelled"), not of anything renameable per-project.
        # Determined BEFORE constructing `data` — mirrors update_work_item's
        # `cascading` sequencing exactly, so `data.status` below can exclude
        # itself from the plain PATCH the same way `data.state` does there.
        cascading = bool(cascade and validated_status in TERMINAL_GROUPS)

        data = UpdateModule(
            name=name,
            description=description,
            start_date=start_date,
            target_date=target_date,
            # Cascading moves `status` via cascade-apply below, not this PATCH.
            status=None if cascading else validated_status,
            lead=lead,
            members=members,
            external_source=external_source,
            external_id=external_id,
        )

        if cascading:
            # cascade-apply writes the module's status AND every eligible item
            # in ONE transaction (M5); the module's status must NOT also go out
            # as a plain PATCH (mirrors update_work_item excluding `state`) —
            # enforced above by constructing `data.status = None` while
            # cascading, not just by this truthiness check.
            # Omitting `item_ids` (the contract's documented headless path)
            # makes the server take every currently-eligible item. Any OTHER
            # field set alongside status+cascade must still land — cascade-apply
            # only ever touches `status`, so it must not silently swallow a
            # caller's other requested changes. Apply them first, status
            # excluded, via the ordinary PATCH; then the apply body carries no
            # item_ids and this tool returns the cascade-apply response.
            if data.model_dump(exclude_none=True):
                client.modules.update(
                    workspace_slug=workspace_slug, project_id=project_id, module_id=module_id, data=data
                )
            return _cascade_send(
                client,
                "POST",
                f"/workspaces/{workspace_slug}/projects/{project_id}/modules/{module_id}/cascade-apply/",
                json={"status": validated_status},
            )

        return client.modules.update(
            workspace_slug=workspace_slug, project_id=project_id, module_id=module_id, data=data
        )

    @mcp.tool()
    def delete_module(project_id: str, module_id: str, workspace_slug: str | None = None) -> None:
        """
        Delete a module by ID.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            module_id: UUID of the module
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        client.modules.delete(workspace_slug=workspace_slug, project_id=project_id, module_id=module_id)

    @mcp.tool()
    def manage_module_work_items(
        project_id: str,
        module_id: str,
        add_ids: list[str] | None = None,
        remove_ids: list[str] | None = None,
        workspace_slug: str | None = None,
    ) -> None:
        """
        Add or remove work items on a module in a single call.

        At least one of add_ids or remove_ids must be provided.

        Args:
            project_id: UUID of the project
            module_id: UUID of the module
            add_ids: UUIDs of work items to add to the module
            remove_ids: UUIDs of work items to remove from the module
        """
        if not add_ids and not remove_ids:
            raise ValueError("At least one of add_ids or remove_ids must be provided.")
        client, workspace_slug = get_plane_client_context(workspace_slug)
        if add_ids:
            client.modules.add_work_items(
                workspace_slug=workspace_slug,
                project_id=project_id,
                module_id=module_id,
                issue_ids=add_ids,
            )
        if remove_ids:
            for work_item_id in remove_ids:
                client.modules.remove_work_item(
                    workspace_slug=workspace_slug,
                    project_id=project_id,
                    module_id=module_id,
                    work_item_id=work_item_id,
                )

    @mcp.tool()
    def list_module_work_items(
        project_id: str,
        module_id: str,
        pql: Annotated[str | None, Field(description=PQL_FIELD_HINT)] = None,
        order_by: str | None = None,
        per_page: int | None = None,
        cursor: str | None = None,
        expand: str | None = None,
        fields: str | None = None,
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """
        List work items in a module with optional PQL filtering.

        Args:
            project_id: UUID of the project
            module_id: UUID of the module
            pql: PQL filter expression. See field description for syntax.
                Omit to list all items in the module.
            order_by: Field to sort by; prefix with `-` for descending.
            per_page: Results per page, 1-100 (default 25).
            cursor: Pagination cursor from a previous response's `next_cursor`.
            expand: Comma-separated related fields to expand.
            fields: Comma-separated sparse fieldset.

        Returns:
            Paginated envelope with results, total_count, next_cursor, prev_cursor.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        pql_error = guard_pql(client, workspace_slug, pql, "list_module_work_items", project_id)
        if pql_error:
            return pql_error
        params = WorkItemQueryParams(
            pql=pql,
            order_by=order_by,
            per_page=per_page,
            cursor=cursor,
            expand=expand,
            fields=fields,
        )
        try:
            response: PaginatedModuleWorkItemResponse = client.modules.list_work_items(
                workspace_slug=workspace_slug,
                project_id=project_id,
                module_id=module_id,
                params=params,
            )
        except HttpError as e:
            if pql and e.status_code == 400 and isinstance(e.response, dict) and "pql" in e.response:
                logger.warning("list_module_work_items: invalid PQL %r → %s", pql, e.response)
                return {
                    "error": e.response["pql"],
                    "failed_pql": pql,
                    "pql_reference": PQL_FULL_REFERENCE,
                    "hint": "The PQL above failed. Fix it using the reference and retry list_module_work_items.",
                }
            raise
        return {
            "results": [
                item.model_dump() if hasattr(item, "model_dump") else item for item in (response.results or [])
            ],
            "total_count": response.total_count,
            "count": response.count,
            "next_cursor": response.next_cursor,
            "prev_cursor": response.prev_cursor,
            "next_page_results": response.next_page_results,
            "prev_page_results": response.prev_page_results,
        }

    @mcp.tool()
    def manage_module_archive(
        project_id: str, module_id: str, archive: bool, workspace_slug: str | None = None
    ) -> None:
        """
        Archive or unarchive a module.

        Args:
            project_id: UUID of the project
            module_id: UUID of the module
            archive: True to archive the module, False to unarchive it
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        if archive:
            client.modules.archive(workspace_slug=workspace_slug, project_id=project_id, module_id=module_id)
        else:
            client.modules.unarchive(workspace_slug=workspace_slug, project_id=project_id, module_id=module_id)
