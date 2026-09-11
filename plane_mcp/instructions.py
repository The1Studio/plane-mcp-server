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

## Workspace bootstrap (create_workspace -> invite -> create project)

Standing up a workspace from scratch. Both write steps are irreversible enough
to need confirmation first: create_workspace makes a top-level tenant, and
invite_workspace_member creates a pending membership grant that nobody is told
about.

1. create_workspace(name, slug, organization_size=None) -- instance-admin only,
   and only on a server carrying the fork's POST /api/v1/workspaces/. A 403
   names which of the two reasons applies: not an instance admin, or creation
   disabled on the instance. Until The1Studio/plane#108 is deployed this answers
   404; the error says so.
2. Then BOTH of these, or the new workspace is unreachable next session:
   - set_workspace("<slug>") to retarget this session (the tool does NOT do this
     for you -- it must not silently move where later calls land);
   - add "<slug>" to PLANE_WORKSPACE_SLUGS in the MCP server config and restart
     the server. Without it the next session cannot address the workspace at all
     -- the same declared-slug trap as workspace discovery.
3. invite_workspace_member(email, role) -- owner-only, ASYNC, and SENDS NO
   EMAIL. The v1 endpoint it calls records the invite and dispatches nothing
   (only the web app's own invite flow does; The1Studio/plane#109), so the
   invitee is NOT notified: they see the pending invite only when they next
   sign in to Plane, and otherwise must be told out of band. Say that to the
   user instead of reporting that someone was emailed. The invite adds the
   person to the invite list, not to the members; they appear in
   get_workspace_members only after accepting. Check with
   list_workspace_invites; a repeat invite for the same address returns
   already_invited rather than creating a second one, and an accepted one
   cannot be revoked (that is a membership change, not an invite change).
4. create_project(workspace_slug="<slug>", ...) per project, then add members.

## GitHub status-automation config (get_github_state_config / set_github_state_config)

Three tiers, most-specific-wins: built-in defaults -> instance-wide "global" ->
workspace -> project. get_github_state_config ALWAYS returns the fully RESOLVED
rules at the tier you ask for, not just that tier's stored override — a
project-tier read already has global + workspace + project merged in. Only
set_github_state_config writes a single tier; write the lowest tier that
actually needs to change instead of repeating rules already correct below it.
"""
