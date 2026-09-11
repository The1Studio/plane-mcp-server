"""Workspace-related tools for Plane MCP Server."""

import posixpath
from typing import Any
from urllib.parse import quote, unquote

import httpx
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
from plane_mcp.tools.workload import _send

# Workspace-invitation route segment. The plane-sdk models no invitation
# resource at all (no `invite` in its WorkspacesAPI), so invitations go
# through the raw `_send` helper instead of an SDK method -- the same choice
# projects.py makes for the fork-owned `project_ext` endpoints. Named once so
# a rename is a one-line change.
_INVITATIONS_PATH_SEGMENT = "invitations"

# Instance-level workspace-collection route. Deliberately NOT slug-scoped:
# this is the one workspace tool that targets the instance, not a workspace.
_WORKSPACES_COLLECTION_PATH = "/workspaces/"


def _owner_only_error() -> RuntimeError:
    """Explain a 403 from the invitation endpoints.

    `WorkspaceInvitationsViewset` is gated by `WorkspaceOwnerPermission`, not
    the workspace-admin check most other endpoints use -- so a key whose user
    is a workspace Admin (role 20) but not the workspace *owner* is refused
    here while every neighbouring tool succeeds.

    Plane answers the same 403 for a slug the key cannot reach at all, so this
    does NOT claim to disambiguate the two; it names both possibilities and
    leaves the judgement to the caller.
    """
    return RuntimeError(
        "Workspace invitations require workspace OWNER permission: the endpoint is "
        "gated by Plane's WorkspaceOwnerPermission, not the workspace-admin check "
        "other tools use, so a key whose user is an Admin (role 20) but not the "
        "owner is refused. Plane returns this same 403 for a workspace slug the key "
        "cannot reach at all, so this is not proof of an owner-permission problem "
        "-- confirm the slug is one your account is a member of before concluding "
        "the key is under-privileged."
    )


def _invitation_error(exc: httpx.HTTPStatusError) -> RuntimeError | None:
    """Turn the invite serializer's 400 field errors into one legible sentence.

    `WorkspaceInviteSerializer` rejects the three likeliest mistakes with DRF
    field errors -- `{"email": ["Invalid email address"]}`, `{"role": ["Invalid
    role"]}`, `{"non_field_errors": ["Email already invited"]}`. None carries an
    `error` key, so the shared `_send` helper's extraction finds nothing, the
    custom raise is skipped, and the caller reaches
    `response.raise_for_status()`: httpx's own "400 Bad Request for <url>"
    followed by a developer.mozilla.org link, with nothing about the address or
    the role. Parsing the body here is what keeps the reason visible -- the same
    gap `_workspace_create_error` closes for `create_workspace`.

    Deliberately narrow, in two directions, so it only ever adds information:

    * a non-400 is not this function's business -- other statuses keep the
      handling their callers already give them (403 -> owner message, 404 ->
      raw `httpx.HTTPStatusError`);
    * a body that DOES carry a usable `error` key is left alone, because `_send`
      already folded it (plus any `error_code`) into a richer message than this
      could build. That is the shape the revoke endpoint returns, which is why
      `revoke_workspace_invite`'s 400 keeps raising `httpx.HTTPStatusError`.

    Returns None when the caller should keep the original exception.
    """
    if exc.response.status_code != 400:
        return None
    try:
        body = exc.response.json()
    except Exception:  # noqa: BLE001 - non-JSON error body; nothing to add
        return None
    if not isinstance(body, dict) or not body or body.get("error"):
        return None

    reasons = body.get("non_field_errors")
    if isinstance(reasons, list) and reasons:
        # A cross-field rejection ("Email already invited") reads best on its
        # own -- prefixing it with the DRF field name adds nothing.
        detail = "; ".join(str(reason) for reason in reasons)
    else:
        flat: list[str] = []
        for field, reason in body.items():
            values = reason if isinstance(reason, list) else [reason]
            flat.extend(f"{field}: {value}" for value in values)
        detail = "; ".join(flat)
    return RuntimeError(f"The server rejected the invitation [400]: {detail}")


def _invitation_request(client: Any, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
    """`_send` for invitation routes, with the owner-permission 403 explained.

    403 is translated into `_owner_only_error` and 400 into the serializer's
    field errors (`_invitation_error`). A 404 stays a raw
    `httpx.HTTPStatusError` on purpose: this endpoint ships upstream
    (makeplane/plane and the fork alike), so a 404 here does NOT mean a missing
    fork app and must not be reported as one --
    `tests/test_project_admin.py::test_non_404_error_is_not_masked` pins that
    distinction for the `project_ext` tools and it holds here too.
    """
    try:
        return _send(client, method, path, json=json)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            raise _owner_only_error() from exc
        field_error = _invitation_error(exc)
        if field_error is not None:
            raise field_error from exc
        raise


def _invite_id_segment(invite_id: str) -> str:
    """Validate and percent-encode an invite id for the DELETE path.

    `revoke_workspace_invite` is the first place a caller-supplied *resource id*
    -- not an internally generated UUID -- lands in a path segment, and the tool
    is a DELETE, so a `..` or a `/` in the argument must not be able to reshape
    which resource is addressed.

    Encoding is necessary but NOT sufficient, which is the trap here. httpx
    percent-DECODES a URL before it normalizes the path, so:

    * `quote("..", safe="")` is `".."` -- dots are unreserved in RFC 3986 -- and
      the segment still collapses, `.../invitations/../` -> `/workspaces/<slug>/`;
    * `%2F` decodes back to a real separator, so the traversal resolves anyway.
      Probed: `x/../../workspaces/other/invitations/y` reached
      `/api/v1/workspaces/<slug>/workspaces/other/invitations/y/`.

    So the check names the property that actually matters, on the DECODED value:
    interpolating the id must not change the templated path's structure. `..`
    and `.` are caught because they collapse the tail away, a slash because it
    is a separator regardless of what it was encoded as, and every other dot-run
    spelling falls out of the same comparison instead of needing its own case.
    A trailing slash is refused too -- it is not a segment an id can be.
    """
    segment = quote(invite_id.strip(), safe="")
    decoded = unquote(segment)
    # No trailing slash on the probe: `normpath` drops one, which would fail
    # every ordinary id. The injected segment is what the comparison is about.
    probe = f"/workspaces/_/{_INVITATIONS_PATH_SEGMENT}/{decoded}"
    if "/" in decoded or posixpath.normpath(probe) != probe:
        raise ValueError(
            f"Invalid invite_id {invite_id.strip()!r}: it must be a single path segment, "
            "such as the `id` from list_workspace_invites."
        )
    return segment


def _as_invite_list(payload: Any) -> list[dict[str, Any]]:
    """Normalize the invitations list response into a list of invite dicts.

    The viewset returns a bare array, but `list_workspace_invites` promises the
    caller a list and the dedupe scan iterates it -- so an unexpected shape is
    reported plainly here rather than leaking a dict into code that will call
    `.get` on it or abort with an unrelated `AttributeError`.
    """
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        payload = payload["results"]  # pagination envelope, should the server add one
    if not isinstance(payload, list):
        raise RuntimeError(f"Expected a list of workspace invitations from the server, got {type(payload).__name__}.")
    return [item for item in payload if isinstance(item, dict)]


def _workspace_create_error(exc: httpx.HTTPStatusError) -> RuntimeError:
    """Turn a failed `POST /workspaces/` into a message naming the real cause.

    Three distinct server answers share one status code family and are easy to
    conflate, so each is named explicitly rather than left to a bare status
    line:

    * 403 `INSTANCE_ADMIN_REQUIRED` -- the key's user is not an instance admin.
    * 403 `WORKSPACE_CREATION_DISABLED` -- the instance turned the feature off.
    * 409 `WORKSPACE_SLUG_EXISTS` -- the slug is taken.

    A 409 body is `{"slug": "...", "error_code": "..."}` -- it carries no
    `error` key, so the shared `_send` helper's error extraction finds nothing
    and would degrade to a bare "409 Conflict for <url>". Reading the body here
    is what keeps the reason visible.
    """
    status = exc.response.status_code
    try:
        body = exc.response.json()
    except Exception:  # noqa: BLE001 - non-JSON error body; report the status alone
        body = {}
    if not isinstance(body, dict):
        body = {}
    code = body.get("error_code") or ""

    if status == 403 and code == "WORKSPACE_CREATION_DISABLED":
        return RuntimeError(
            "This Plane instance has workspace creation disabled "
            "[WORKSPACE_CREATION_DISABLED]. An instance admin must enable it."
        )
    if status == 403:
        detail = f" [{code}]" if code else ""
        return RuntimeError(
            f"Creating a workspace requires instance admin permission{detail}. The API key "
            "belongs to a user who is not an instance admin on this Plane deployment."
        )
    if status == 409:
        message = body.get("slug") or body.get("error") or "the slug is already in use"
        return RuntimeError(
            f"Cannot create the workspace: {message} [WORKSPACE_SLUG_EXISTS]. "
            "Pick a different `slug`, or call `set_workspace` with the existing slug "
            "if you meant to target the workspace that already owns it."
        )
    if status == 400:
        # DRF field errors arrive as {"<field>": ["<reason>", ...]} with no
        # `error` key, so `_send` cannot surface them -- without this the caller
        # sees only "400 Bad Request" for the most likely failure of all
        # (a name or slug that fails validation).
        return RuntimeError(f"The server rejected the workspace payload [400]: {body}")
    if status == 404:
        # Until the fork's endpoint is deployed this is the ONLY answer any
        # caller gets, so the default branch's `failed [404]: {}` -- the body is
        # empty because a Django 404 serves HTML and the `json()` above raises
        # into its `except` -- would name no cause and route nowhere. The route
        # is fork-owned (`_WORKSPACES_COLLECTION_PATH`), which is why this must
        # not be folded into `_fork_endpoint_error`: that message names
        # `project_ext` and would send the caller chasing the wrong app.
        return RuntimeError(
            "Creating a workspace failed [404]: this server does not expose "
            "POST /api/v1/workspaces/. That route is shipped by the The1Studio Plane "
            "fork (The1Studio/plane#108, still open against `staging`); upstream Plane, "
            "Plane Cloud, and any fork deployment predating it answer 404 here. Nothing "
            "was created -- use an existing workspace, or have the instance upgraded."
        )
    return RuntimeError(f"Creating the workspace failed [{status}]: {body}")


def _discover_workspaces() -> list[dict] | None:
    """
    Ask the server which workspaces the credentials can reach.

    Returns a list of {slug, name, id} on success, or None when the instance
    does not expose the discovery endpoint (stock Plane predating
    The1Studio/plane#30) -- the caller then falls back to probing.

    Routed through the SDK's own request helper rather than a hand-built HTTP
    call so base-URL resolution and API-key auth stay in one place; `get_me`
    reaches /users/me the same way. Crucially this needs NO workspace, which is
    the entire point: discovery has to work on a server configured with no slug.
    """
    client, _ = get_plane_client_context(require_workspace=False)
    try:
        response = client.users._get("me/workspaces")
    except Exception:  # noqa: BLE001 - absent endpoint or unreachable host
        return None

    if not isinstance(response, list):
        return None

    entries: list[dict] = []
    for item in response:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if slug:
            entries.append({"slug": slug, "name": item.get("name"), "id": item.get("id")})
    return entries


def register_workspace_tools(mcp: FastMCP) -> None:
    """Register all workspace-related tools with the MCP server."""

    @mcp.tool()
    def list_workspaces(candidates: list[str] | None = None) -> dict:
        """
        List the workspaces these credentials can reach.

        Prefers real discovery: GET /api/v1/users/me/workspaces/ returns every
        workspace the account is an active member of. When the server exposes
        it, you get the complete list and need supply nothing.

        FALLBACK: a Plane instance without that endpoint (stock, predating
        The1Studio/plane#30) cannot enumerate workspaces at all -- no public
        route lists them, and the web app's internal one rejects API keys. There
        this degrades to PROBING the configured slugs plus any candidates you
        name, and a workspace missing from the result is missing from that set,
        not necessarily from your account. `discovery` in the result says which
        mode ran, so an empty list is never mistaken for "you have none".

        FINDING YOUR SLUG when probing: it is the first path segment when you
        are logged into Plane -- <base-url>/<slug>/projects/.

        UNREACHABLE IS AMBIGUOUS when probing: Plane answers a wrong slug and a
        real slug you lack access to with the same 403, so `reachable: false`
        means "this slug did not work", never "this workspace does not exist".

        Args:
            candidates: Extra slugs to probe. Only used in fallback mode --
                discovery needs no hints.

        Returns:
            dict with `discovery` ("api" or "probe"), `active` (session
            default), `default` (env default), `workspaces`, and `note` when
            there was nothing to probe.
        """
        discovered = _discover_workspaces()
        if discovered is not None:
            return {
                "discovery": "api",
                "active": get_active_workspace(),
                "default": (get_configured_workspace_slugs() or [None])[0],
                "workspaces": [{**w, "source": "discovered", "reachable": True} for w in discovered],
                "note": (
                    "Complete list of workspaces this account is an active member of. "
                    "Pass a slug to set_workspace() to make it the session default."
                )
                if discovered
                else (
                    "The server reports this account is an active member of no workspaces. "
                    "This is a real answer from the API, not a failed probe."
                ),
            }

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
            "discovery": "probe",
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

    @mcp.tool()
    def list_workspace_invites(workspace_slug: str | None = None) -> list[dict[str, Any]]:
        """
        List the pending and past invitations to a workspace.

        Invitations are a separate resource from membership: an invite exists
        from the moment it is recorded until the person accepts or it is
        revoked, and only on acceptance does the person appear in
        `get_workspace_members`. So a person who says "I was invited" but is
        absent from the member list is usually visible here.

        This is also the only way to tell whether an invite reached anyone: the
        tool that creates one sends no email, so a pending row here means the
        person has not been notified (see `invite_workspace_member`).

        Requires workspace OWNER permission -- see `invite_workspace_member`
        for the caveat, which applies to every invitation tool.

        Args:
            workspace_slug: Address a workspace other than the session default.

        Returns:
            List of invite objects, each with `id`, `email`, `role`,
            `created_at`, `updated_at`, `responded_at`, and `accepted`.
            `accepted` false with a null `responded_at` means still pending.
        """
        client, workspace_slug = get_plane_client_context(workspace_slug)
        path = f"/workspaces/{workspace_slug}/{_INVITATIONS_PATH_SEGMENT}/"
        return _as_invite_list(_invitation_request(client, "GET", path))

    @mcp.tool()
    def invite_workspace_member(
        email: str,
        role: int = 15,
        workspace_slug: str | None = None,
    ) -> dict[str, Any]:
        """
        Invite someone to a workspace by email.

        WRITE OPERATION THAT CREATES A PENDING MEMBERSHIP GRANT -- AND SENDS NO
        EMAIL. Calling this records an invitation that grants access to `email`
        once accepted; it does NOT notify anyone. The v1 endpoint behind this
        tool
        (`POST /api/v1/workspaces/{slug}/invitations/`) writes the row and
        returns it; only the web app's own invite flow dispatches the
        `workspace_invitation` task (The1Studio/plane#109). So the invitee learns
        nothing from this call -- they see the pending invite only when they next
        sign in to Plane and open their invitations page, and otherwise must be
        told out of band. Confirm the address, the role, and the target workspace
        with the user before calling it, and tell them to notify the person
        themselves.

        Nothing is sent, so the side effect is silent: the row exists and grants
        access on acceptance, and a row nobody acts on is never cleaned up. Check
        with `list_workspace_invites` rather than assuming the person was reached.

        The invitation is asynchronous -- this adds the person to the workspace's
        invite list, NOT to its members. They appear in `get_workspace_members`
        only after accepting. Do not follow this with a project-membership call
        expecting it to work; invite first, confirm acceptance, then add projects.

        Idempotent per email: if an invite for this address already exists it is
        returned with `already_invited: true` and nothing is created. Matching
        ignores case and surrounding whitespace, so `A@x.com` and `a@x.com` are
        treated as the same person rather than invited twice. This check is by
        email only and the server makes the same one -- neither notices that the
        address is ALREADY A MEMBER, which the server accepts as a fresh invite.

        Requires workspace OWNER permission. This endpoint is gated by Plane's
        `WorkspaceOwnerPermission`, not the workspace-admin check most other
        tools use, so an API key whose user is an Admin (role 20) but not the
        workspace owner is refused with a 403 that names this. Plane returns
        that same 403 for a slug the key cannot reach at all, so the error does
        not claim to tell the two apart.

        Args:
            email: Address to invite.
            role: Workspace role -- 20 (Admin), 15 (Member, default), or 5 (Guest).
            workspace_slug: Address a workspace other than the session default.

        Returns:
            The created invite object (`id`, `email`, `role`, `created_at`,
            `updated_at`, `responded_at`, `accepted`), or on a repeat call that
            same object plus `already_invited: true` and a `note` saying no new
            invitation was created.
        """
        if not email or not email.strip():
            raise ValueError("invite_workspace_member requires a non-empty email")

        client, workspace_slug = get_plane_client_context(workspace_slug)
        path = f"/workspaces/{workspace_slug}/{_INVITATIONS_PATH_SEGMENT}/"

        wanted = email.strip().lower()
        for invite in _as_invite_list(_invitation_request(client, "GET", path)):
            if str(invite.get("email") or "").strip().lower() == wanted:
                # `**invite` comes FIRST: spread last it would let a server row
                # carrying its own `already_invited` override the `True` this
                # branch just determined, so the field could state the opposite
                # of what the code did. The tool's own key wins.
                return {
                    **invite,
                    "already_invited": True,
                    "note": (
                        "An invitation for this email already exists on the workspace, so no "
                        "new one was sent. `accepted: false` with a null `responded_at` means it "
                        "is still pending; `accepted: true` means the person is already a member."
                    ),
                }

        return _invitation_request(client, "POST", path, json={"email": email.strip(), "role": role})

    @mcp.tool()
    def revoke_workspace_invite(invite_id: str, workspace_slug: str | None = None) -> dict[str, Any]:
        """
        Revoke a workspace invitation.

        WRITE OPERATION, and it revokes access: use `list_workspace_invites` to
        find the `id` first, and confirm with the user which address it belongs
        to before calling it.

        Only a still-open invitation can be revoked. The server refuses with a
        400 when the invite was already accepted or already responded to --
        removing an accepted person is a membership change, not an invite
        change, and is not this tool.

        Requires workspace OWNER permission -- see `invite_workspace_member`.

        Args:
            invite_id: `id` of the invitation, from `list_workspace_invites`.
                Must be a single path segment; anything that would not survive
                as one is refused rather than sent.
            workspace_slug: Address a workspace other than the session default.

        Returns:
            dict with `revoked: true` and the `invite_id` that was removed.
        """
        if not invite_id or not invite_id.strip():
            raise ValueError("revoke_workspace_invite requires a non-empty invite_id")
        segment = _invite_id_segment(invite_id)

        client, workspace_slug = get_plane_client_context(workspace_slug)
        path = f"/workspaces/{workspace_slug}/{_INVITATIONS_PATH_SEGMENT}/{segment}/"
        _invitation_request(client, "DELETE", path)
        return {"revoked": True, "invite_id": invite_id.strip()}

    @mcp.tool()
    def create_workspace(
        name: str,
        slug: str,
        organization_size: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a new workspace on this Plane instance.

        WRITE OPERATION THAT IS NOT REVERSIBLE VIA THE API: this creates a
        top-level tenant that every workspace-scoped tool then accepts as a
        slug, and there is no API route to delete it. Confirm the name and slug
        with the user before calling it.

        Requires instance admin -- a key belonging to an ordinary workspace
        Admin is refused with a 403 naming that. The instance may also have
        workspace creation disabled outright, which is reported separately by
        its `error_code` so "not allowed" is never misread as "slug taken".

        This targets `POST /api/v1/workspaces/`, an endpoint the The1Studio
        Plane fork ships separately from this MCP server
        (The1Studio/plane#107; the PR delivering it, #108, is still open against
        `staging`). Against upstream Plane / Plane Cloud, or a fork deployment
        predating it, the call fails with a 404 naming this -- and until #108 is
        deployed, that 404 is the answer every caller gets.

        Args:
            name: Display name of the workspace, up to 80 characters.
            slug: URL slug, up to 48 characters, letters/digits/underscore/hyphen
                only, and it must not already be in use.
            organization_size: Optional size bucket, e.g. "1-10". Sent as JSON
                `null` when omitted, which the server accepts.

        Returns:
            The created workspace (`id`, `name`, `slug`, `owner`,
            `organization_size`, `logo_url`, `created_at`, `updated_at`) with
            `role: 20` (you are its Owner) and `total_members: 1`, plus
            `next_steps` naming the two follow-ups that are easy to miss.
        """
        if not name or not name.strip():
            raise ValueError("create_workspace requires a non-empty name")
        if not slug or not slug.strip():
            raise ValueError("create_workspace requires a non-empty slug")

        # No workspace exists yet to scope the call to, so this is one of the
        # rare genuinely workspace-independent calls (`get_me` is the other).
        client, _ = get_plane_client_context(require_workspace=False)

        payload: dict[str, Any] = {
            "name": name.strip(),
            "slug": slug.strip(),
            "organization_size": organization_size,
        }
        try:
            created = _send(client, "POST", _WORKSPACES_COLLECTION_PATH, json=payload)
        except httpx.HTTPStatusError as exc:
            raise _workspace_create_error(exc) from exc

        # A 201 whose body is empty (`_send` returns None for an empty body) or
        # is not an object cannot be reported as a success: the docstring
        # promises the created workspace back, `next_steps` must quote the
        # SERVER's slug (the server normalizes it), and falling back to the
        # caller's input slug would instruct `set_workspace('<input>')` for a
        # workspace that was never read. `_as_invite_list` raises for the same
        # reason -- silence would be indistinguishable from success.
        if not isinstance(created, dict) or not created.get("slug"):
            raise RuntimeError(
                "The server answered 201 for the workspace but returned no usable body "
                f"(got {type(created).__name__})"
                + (f" {created!r}" if created else "")
                + ". The workspace may have been created; call list_workspaces() to "
                "check before retrying, so a retry does not create a second one."
            )

        result = dict(created)
        new_slug = result["slug"]
        result["next_steps"] = [
            f"Call set_workspace({new_slug!r}) to make it the session default for later calls.",
            (
                f"Add {new_slug!r} to PLANE_WORKSPACE_SLUGS in the MCP server config (or "
                "PLANE_WORKSPACE_SLUG for a single-workspace setup) and restart the server. "
                "Without that the next session cannot address this workspace at all -- the "
                "same trap documented for workspace discovery."
            ),
        ]
        return result
