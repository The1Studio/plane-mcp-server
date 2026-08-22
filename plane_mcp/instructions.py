"""Server-level instructions sent once to MCP clients (FastMCP `instructions` param)."""

SERVER_INSTRUCTIONS = """
## Epics

There are no epic tools — an epic is a work item whose type is named "Epic". Work
items always belong to a project; ask which if one is not named.
1. type = resolve_work_item_type(project_id, "Epic") — type.id is the type_id.
2. Create: create_work_item(project_id, type_id=type.id, name=...).
3. List: list_work_items(project_id, pql='type = "<type id>"').
4. Read / update / delete / nest: retrieve_work_item / update_work_item /
   delete_work_item by work item id (set parent=<work item id> to nest).
5. List an epic's children: list_work_items(project_id, pql='childOf("<EPIC-IDENTIFIER>")')
   using the epic's human-readable identifier (e.g. "PROJ-12") from retrieve_work_item.

## GitHub status-automation config (get_github_state_config / set_github_state_config)

Three tiers, most-specific-wins: built-in defaults -> instance-wide "global" ->
workspace -> project. get_github_state_config ALWAYS returns the fully RESOLVED
rules at the tier you ask for, not just that tier's stored override — a
project-tier read already has global + workspace + project merged in. Only
set_github_state_config writes a single tier; write the lowest tier that
actually needs to change instead of repeating rules already correct below it.
"""
