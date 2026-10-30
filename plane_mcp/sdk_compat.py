"""Local response shims for plane-sdk models that are narrower than the API payload.

plane-sdk (0.2.20, and still 0.3.2) validates some responses against types that
reject what the server actually sends:

* `WorkItemRelationResponse` types all eight relation lists as required
  `list[str]`, but the API returns `{project_id, issue_id}` objects
  (plane-mcp-server#37).
* `WorkItemDetail.created_by` / `.updated_by` are `str | None`, but the API
  returns an object when the caller passes `expand=created_by` (#36).

A pin bump does not help, so these helpers issue the SAME request the SDK
method issues, through the SDK resource's own session (`_get`), and validate
against permissive local subclasses. Delete this module once the SDK widens
the types upstream.
"""

from typing import Any

from plane.models.query_params import RetrieveQueryParams
from plane.models.work_items import WorkItemDetail
from pydantic import BaseModel, ConfigDict, Field

# Fields of WorkItemDetail whose expanded form is an object, not a string.
_EXPANDABLE_USER_FIELDS = ("created_by", "updated_by")


class WorkItemRelationsCompat(BaseModel):
    """Relation lists, each entry a UUID string or a `{project_id, issue_id}` object."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    blocking: list[str | dict[str, Any]] = Field(default_factory=list)
    blocked_by: list[str | dict[str, Any]] = Field(default_factory=list)
    duplicate: list[str | dict[str, Any]] = Field(default_factory=list)
    relates_to: list[str | dict[str, Any]] = Field(default_factory=list)
    start_after: list[str | dict[str, Any]] = Field(default_factory=list)
    start_before: list[str | dict[str, Any]] = Field(default_factory=list)
    finish_after: list[str | dict[str, Any]] = Field(default_factory=list)
    finish_before: list[str | dict[str, Any]] = Field(default_factory=list)


class WorkItemDetailCompat(WorkItemDetail):
    """WorkItemDetail whose creator/updater accept the expanded object form."""

    created_by: str | dict[str, Any] | None = None
    updated_by: str | dict[str, Any] | None = None


def list_work_item_relations(
    client: Any, workspace_slug: str, project_id: str, work_item_id: str
) -> WorkItemRelationsCompat:
    """Same GET as `client.work_items.relations.list`, with a permissive model."""
    response = client.work_items.relations._get(
        f"{workspace_slug}/projects/{project_id}/work-items/{work_item_id}/relations"
    )
    return WorkItemRelationsCompat.model_validate(response)


def expands_user_fields(expand: str | None) -> bool:
    """True when `expand` asks for a field the SDK types as a bare string."""
    if not expand:
        return False
    requested = {part.strip() for part in expand.split(",")}
    return any(name in requested for name in _EXPANDABLE_USER_FIELDS)


def retrieve_work_item(
    client: Any,
    workspace_slug: str,
    project_id: str,
    work_item_id: str,
    params: RetrieveQueryParams | None = None,
) -> WorkItemDetailCompat:
    """Same GET as `client.work_items.retrieve`, with a permissive model."""
    query_params = params.model_dump(exclude_none=True) if params else None
    response = client.work_items._get(
        f"{workspace_slug}/projects/{project_id}/work-items/{work_item_id}",
        params=query_params,
    )
    return WorkItemDetailCompat.model_validate(response)
