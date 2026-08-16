"""Plane client initialization for MCP server."""

import os
from contextvars import ContextVar
from typing import NamedTuple

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger
from plane import PlaneClient

logger = get_logger(__name__)

# Session-scoped workspace override set by the `set_active_workspace` tool.
# A ContextVar (not a module global) so concurrent HTTP-transport requests
# cannot leak one caller's active workspace into another's.
_active_workspace: ContextVar[str | None] = ContextVar("active_workspace", default=None)


class PlaneClientContext(NamedTuple):
    """Context containing Plane client and workspace information."""

    client: PlaneClient
    workspace_slug: str


def set_active_workspace(slug: str | None) -> None:
    """Set (or clear, with None) the session-default workspace slug."""
    _active_workspace.set(slug.strip() if slug and slug.strip() else None)


def get_active_workspace() -> str | None:
    """Return the session-default workspace slug, if one has been set."""
    return _active_workspace.get()


def get_configured_workspace_slugs() -> list[str]:
    """
    Return the workspace slugs this server was configured with, in order.

    Sourced from PLANE_WORKSPACE_SLUGS (comma- or newline-separated), with
    PLANE_WORKSPACE_SLUG prepended so the single-workspace configuration keeps
    working unchanged.

    This is a *declared* list, not a discovered one: Plane's public API exposes
    no workspace-listing endpoint (plane-sdk's WorkspacesAPI has no `list()` --
    every method takes workspace_slug as an input), so no amount of API access
    can enumerate the workspaces a user belongs to. `list_workspaces` validates
    this declared set against the API rather than inventing a discovery it
    cannot perform.
    """
    raw = os.getenv("PLANE_WORKSPACE_SLUGS", "")
    slugs = [s.strip() for s in raw.replace("\n", ",").split(",") if s.strip()]

    default = os.getenv("PLANE_WORKSPACE_SLUG", "").strip()
    if default:
        slugs.insert(0, default)

    seen: set[str] = set()
    ordered: list[str] = []
    for slug in slugs:
        if slug not in seen:
            seen.add(slug)
            ordered.append(slug)
    return ordered


class MissingWorkspaceError(ValueError):
    """Raised when a workspace-scoped call has no workspace slug to target."""


_MISSING_WORKSPACE_HELP = (
    "No Plane workspace slug resolved for this call. This server can run without "
    "PLANE_WORKSPACE_SLUG set, so name a workspace one of these ways:\n"
    "  - pass `workspace_slug` on this call, or\n"
    "  - call `set_workspace('<slug>')` once to set a session default, or\n"
    "  - set PLANE_WORKSPACE_SLUG in the server environment.\n"
    "Your slug is the first path segment when you are logged into Plane: "
    "<base-url>/<slug>/projects/. Plane's public API exposes no workspace-listing "
    "endpoint, so it cannot be discovered automatically -- `list_workspaces` can "
    "probe candidate slugs for you."
)


def get_plane_client_context(workspace_slug: str | None = None, require_workspace: bool = True) -> PlaneClientContext:
    """
    Initialize and return a PlaneClient instance with workspace context.

    Authentication is handled by the PlaneOAuthProvider, which supports:
    1. Environment variables (PLANE_API_KEY + PLANE_WORKSPACE_SLUG)
    2. HTTP headers (x-api-key + x-workspace-slug)
    3. OAuth access token

    Workspace resolution order (first non-empty wins):
    1. The explicit `workspace_slug` argument -- a per-call override
    2. The session default set via `set_active_workspace`
    3. The OAuth access token's `workspace_slug` claim
    4. PLANE_WORKSPACE_SLUG

    Environment variables:
    - PLANE_INTERNAL_BASE_URL: Internal URL for Plane API (preferred for server-to-server calls)
    - PLANE_BASE_URL: Base URL for Plane API (fallback, default: https://api.plane.so)
    - PLANE_WORKSPACE_SLUG: Default workspace slug
    - PLANE_WORKSPACE_SLUGS: Additional slugs this server may address

    Args:
        workspace_slug: Address a workspace other than the session default for
            this call only. Omit to use the resolution order above.
        require_workspace: Raise MissingWorkspaceError when the order above
            resolves nothing. Pass False for the rare call that is genuinely
            workspace-independent (e.g. `get_me`, which hits /users/me/), so a
            server started without PLANE_WORKSPACE_SLUG can still verify its
            credentials.

    Returns:
        PlaneClientContext containing configured PlaneClient instance and workspace slug

    Raises:
        MissingWorkspaceError: If require_workspace and no slug could be resolved
    """
    base_url = os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so")
    resolved_slug = os.getenv("PLANE_WORKSPACE_SLUG", "")

    api_key = os.getenv("PLANE_API_KEY", "")
    access_token = None

    # Get access token from the OAuth provider (which handles all auth methods)
    stored_access_token: AccessToken | None = get_access_token()
    if stored_access_token:
        # Determine authentication method to use appropriate PlaneClient constructor
        auth_method = stored_access_token.claims.get("auth_method", "oauth")
        token = stored_access_token.token
        resolved_slug = stored_access_token.claims.get("workspace_slug", "")

        # For API key auth methods, use api_key parameter; for OAuth, use access_token
        if auth_method in ("api_key_env", "api_key_header"):
            api_key = token
        else:
            access_token = token

    # Caller-supplied and session-default overrides win over the ambient config.
    active = get_active_workspace()
    if active:
        resolved_slug = active
    if workspace_slug and workspace_slug.strip():
        resolved_slug = workspace_slug.strip()

    resolved_slug = (resolved_slug or "").strip()
    if require_workspace and not resolved_slug:
        # Fail here, with instructions, rather than handing the SDK an empty
        # slug -- that builds a `/workspaces//...` URL and surfaces as an
        # opaque 404 that reads like the resource is missing.
        raise MissingWorkspaceError(_MISSING_WORKSPACE_HELP)

    if access_token:
        client = PlaneClient(
            base_url=base_url,
            access_token=access_token,
        )
    else:
        client = PlaneClient(
            base_url=base_url,
            api_key=api_key,
        )

    return PlaneClientContext(
        client=client,
        workspace_slug=resolved_slug,
    )
