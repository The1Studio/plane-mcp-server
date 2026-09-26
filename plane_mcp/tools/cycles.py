"""Cycle-related tools for Plane MCP Server."""

from datetime import date, timedelta
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.utilities.logging import get_logger
from plane.errors.errors import HttpError
from plane.models.cycles import (
    CreateCycle,
    Cycle,
    PaginatedArchivedCycleResponse,
    PaginatedCycleResponse,
    PaginatedCycleWorkItemResponse,
    TransferCycleWorkItemsRequest,
    UpdateCycle,
)
from plane.models.query_params import WorkItemQueryParams
from pydantic import Field

from plane_mcp.client import get_plane_client_context
from plane_mcp.pql_support import guard_pql
from plane_mcp.tools.pql_reference import PQL_FIELD_HINT, PQL_FULL_REFERENCE

logger = get_logger(__name__)


def _as_date(value: str | None) -> date | None:
    """Parse a Plane date string to a `date`, ignoring any time component.

    Plane's cycle API returns `end_date` as a full ISO-8601 datetime
    (`"2026-09-17T00:00:00Z"`) whenever the field is populated — even though
    the value is written from a bare `YYYY-MM-DD`. Compare these as DATES;
    comparing the raw strings makes a cycle ending today look like it ends in
    the future (see `manage_cycle_archive`).

    Returns None for an absent or unparseable value, which callers treat as
    "no usable end_date" rather than as a comparison against a garbage string.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        logger.warning("Unparseable cycle end_date %r — treating as unset", value)
        return None


def register_cycle_tools(mcp: FastMCP) -> None:
    """Register all cycle-related tools with the MCP server."""

    @mcp.tool()
    def list_cycles(
        project_id: str,
        archived: bool = False,
        params: dict[str, Any] | None = None,
        workspace_slug: str | None = None,
    ) -> list[Cycle]:
        """
        List cycles in a project.

        Args:
            project_id: UUID of the project
            archived: Set True to list archived cycles instead of active ones.
            params: Optional query parameters as a dictionary

        Returns:
            List of Cycle objects
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        if archived:
            archived_response: PaginatedArchivedCycleResponse = client.cycles.list_archived(
                workspace_slug=workspace_slug, project_id=project_id, params=params
            )
            return archived_response.results
        response: PaginatedCycleResponse = client.cycles.list(
            workspace_slug=workspace_slug, project_id=project_id, params=params
        )
        return response.results

    @mcp.tool()
    def create_cycle(
        project_id: str,
        name: str,
        owned_by: str,
        description: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        external_source: str | None = None,
        external_id: str | None = None,
        timezone: str | None = None,
        workspace_slug: str | None = None,
    ) -> Cycle:
        """
        Create a new cycle.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            name: Cycle name
            owned_by: UUID of the user who owns the cycle
            description: Cycle description
            start_date: Cycle start date (ISO 8601 format)
            end_date: Cycle end date (ISO 8601 format)
            external_source: External system source name
            external_id: External system identifier
            timezone: Cycle timezone

        Returns:
            Created Cycle object
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)

        data = CreateCycle(
            name=name,
            owned_by=owned_by,
            description=description,
            start_date=start_date,
            end_date=end_date,
            external_source=external_source,
            external_id=external_id,
            timezone=timezone,
            project_id=project_id,
        )

        return client.cycles.create(workspace_slug=workspace_slug, project_id=project_id, data=data)

    @mcp.tool()
    def retrieve_cycle(project_id: str, cycle_id: str, workspace_slug: str | None = None) -> Cycle:
        """
        Retrieve a cycle by ID.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            cycle_id: UUID of the cycle

        Returns:
            Cycle object
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        return client.cycles.retrieve(workspace_slug=workspace_slug, project_id=project_id, cycle_id=cycle_id)

    @mcp.tool()
    def update_cycle(
        project_id: str,
        cycle_id: str,
        name: str | None = None,
        description: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        owned_by: str | None = None,
        external_source: str | None = None,
        external_id: str | None = None,
        timezone: str | None = None,
        workspace_slug: str | None = None,
    ) -> Cycle:
        """
        Update a cycle by ID.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            cycle_id: UUID of the cycle
            name: Cycle name
            description: Cycle description
            start_date: Cycle start date (ISO 8601 format)
            end_date: Cycle end date (ISO 8601 format)
            owned_by: UUID of the user who owns the cycle
            external_source: External system source name
            external_id: External system identifier
            timezone: Cycle timezone

        Returns:
            Updated Cycle object
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)

        data = UpdateCycle(
            name=name,
            description=description,
            start_date=start_date,
            end_date=end_date,
            owned_by=owned_by,
            external_source=external_source,
            external_id=external_id,
            timezone=timezone,
        )

        return client.cycles.update(workspace_slug=workspace_slug, project_id=project_id, cycle_id=cycle_id, data=data)

    @mcp.tool()
    def delete_cycle(project_id: str, cycle_id: str, workspace_slug: str | None = None) -> None:
        """
        Delete a cycle by ID.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            cycle_id: UUID of the cycle
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        client.cycles.delete(workspace_slug=workspace_slug, project_id=project_id, cycle_id=cycle_id)

    @mcp.tool()
    def manage_cycle_work_items(
        project_id: str,
        cycle_id: str,
        add_ids: list[str] | None = None,
        remove_ids: list[str] | None = None,
        workspace_slug: str | None = None,
    ) -> None:
        """
        Add or remove work items on a cycle in a single call.

        At least one of add_ids or remove_ids must be provided.

        Args:
            project_id: UUID of the project
            cycle_id: UUID of the cycle
            add_ids: UUIDs of work items to add to the cycle
            remove_ids: UUIDs of work items to remove from the cycle
        """
        if not add_ids and not remove_ids:
            raise ValueError("At least one of add_ids or remove_ids must be provided.")
        client, workspace_slug = get_plane_client_context(workspace_slug)
        if add_ids:
            client.cycles.add_work_items(
                workspace_slug=workspace_slug,
                project_id=project_id,
                cycle_id=cycle_id,
                issue_ids=add_ids,
            )
        if remove_ids:
            for work_item_id in remove_ids:
                client.cycles.remove_work_item(
                    workspace_slug=workspace_slug,
                    project_id=project_id,
                    cycle_id=cycle_id,
                    work_item_id=work_item_id,
                )

    @mcp.tool()
    def list_cycle_work_items(
        project_id: str,
        cycle_id: str,
        pql: Annotated[str | None, Field(description=PQL_FIELD_HINT)] = None,
        order_by: str | None = None,
        per_page: int | None = None,
        cursor: str | None = None,
        expand: str | None = None,
        fields: str | None = None,
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """
        List work items in a cycle with optional PQL filtering.

        Args:
            project_id: UUID of the project
            cycle_id: UUID of the cycle
            pql: PQL filter expression. See field description for syntax.
                Omit to list all items in the cycle.
            order_by: Field to sort by; prefix with `-` for descending.
            per_page: Results per page, 1-100 (default 25).
            cursor: Pagination cursor from a previous response's `next_cursor`.
            expand: Comma-separated related fields to expand.
            fields: Comma-separated sparse fieldset.

        Returns:
            Paginated envelope with results, total_count, next_cursor, prev_cursor.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        pql_error = guard_pql(client, workspace_slug, pql, "list_cycle_work_items", project_id)
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
            response: PaginatedCycleWorkItemResponse = client.cycles.list_work_items(
                workspace_slug=workspace_slug,
                project_id=project_id,
                cycle_id=cycle_id,
                params=params,
            )
        except HttpError as e:
            if pql and e.status_code == 400 and isinstance(e.response, dict) and "pql" in e.response:
                logger.warning("list_cycle_work_items: invalid PQL %r → %s", pql, e.response)
                return {
                    "error": e.response["pql"],
                    "failed_pql": pql,
                    "pql_reference": PQL_FULL_REFERENCE,
                    "hint": "The PQL above failed. Fix it using the reference and retry list_cycle_work_items.",
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
    def transfer_cycle_work_items(
        project_id: str,
        cycle_id: str,
        new_cycle_id: str,
        workspace_slug: str | None = None,
    ) -> None:
        """
        Transfer work items from one cycle to another.

        Args:
            workspace_slug: The workspace slug identifier
            project_id: UUID of the project
            cycle_id: UUID of the source cycle
            new_cycle_id: UUID of the target cycle to transfer issues to
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)

        data = TransferCycleWorkItemsRequest(new_cycle_id=new_cycle_id)

        client.cycles.transfer_work_items(
            workspace_slug=workspace_slug,
            project_id=project_id,
            cycle_id=cycle_id,
            data=data,
        )

    @mcp.tool()
    def manage_cycle_archive(project_id: str, cycle_id: str, archive: bool, workspace_slug: str | None = None) -> bool:
        """
        Archive or unarchive a cycle.

        Plane requires the cycle end_date to be strictly in the past before
        archiving. When archive=True, this tool automatically moves end_date
        into the past if the cycle is still active, then archives it.

        Args:
            project_id: UUID of the project
            cycle_id: UUID of the cycle
            archive: True to archive the cycle, False to unarchive it

        Returns:
            True if the operation completed successfully
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        if not archive:
            return client.cycles.unarchive(workspace_slug=workspace_slug, project_id=project_id, cycle_id=cycle_id)

        today = date.today()
        cycle = client.cycles.retrieve(workspace_slug=workspace_slug, project_id=project_id, cycle_id=cycle_id)
        end_date = _as_date(getattr(cycle, "end_date", None))

        # Archive only if the end_date is not already in the past. The
        # comparison is on DATES, not on the raw API strings: Plane returns
        # end_date as `"<date>T00:00:00Z"` whenever one is set, and that text
        # sorts AFTER the bare `"<date>"` we build from today, purely because
        # 'T' is greater than end-of-string. Comparing raw strings therefore
        # read a cycle ending TODAY as ending in the future and skipped the
        # update (plane-mcp-server#53).
        #
        # `>=` rather than `>`: end_date == today is not "in the past"
        # (plane-mcp-server#54).
        if end_date is None or end_date >= today:
            # Yesterday, not today. Plane's archive endpoint rejects the
            # request outright when `end_date >= timezone.now()`
            # (plane/app/views/cycle/archive.py), and the write path anchors a
            # bare date at 00:00 in the PROJECT's timezone — so writing today
            # still compares >= now() for the whole of today, in every
            # timezone, and the archive then fails with "Only completed cycles
            # can be archived". A date strictly before today is the first one
            # that always satisfies the server.
            client.cycles.update(
                workspace_slug=workspace_slug,
                project_id=project_id,
                cycle_id=cycle_id,
                data=UpdateCycle(end_date=(today - timedelta(days=1)).isoformat()),
            )

        return client.cycles.archive(workspace_slug=workspace_slug, project_id=project_id, cycle_id=cycle_id)

    @mcp.tool()
    def complete_cycle(project_id: str, cycle_id: str, workspace_slug: str | None = None) -> Cycle:
        """
        Complete (close) a cycle by moving its end date into the past.

        Plane has no explicit "complete" action — a cycle is considered
        complete when its end_date is in the past. This tool sets end_date to
        yesterday, which closes the cycle and leaves it archivable; setting it
        to *today* would not, because Plane's archive endpoint rejects
        `end_date >= now()` ("Only completed cycles can be archived") and the
        write path anchors a bare date at 00:00 in the project's timezone
        (plane-mcp-server#54).

        Args:
            project_id: UUID of the project
            cycle_id: UUID of the cycle to complete

        Returns:
            Updated Cycle object
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        return client.cycles.update(
            workspace_slug=workspace_slug,
            project_id=project_id,
            cycle_id=cycle_id,
            data=UpdateCycle(end_date=yesterday),
        )
