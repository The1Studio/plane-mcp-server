"""Tests for multi-workspace addressing.

Covers the workspace resolution order and the declared-slug set that
`list_workspaces` validates. Deliberately exercises the FAILURE states too: a
resolution order that silently ignores an override is the bug this feature
exists to prevent, so each precedence rule is pinned by a test that would go red
if the override stopped winning.
"""

import pytest

from plane_mcp.client import (
    MissingWorkspaceError,
    get_active_workspace,
    get_configured_workspace_slugs,
    get_plane_client_context,
    set_active_workspace,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test from a known-empty workspace configuration."""
    for var in ("PLANE_WORKSPACE_SLUG", "PLANE_WORKSPACE_SLUGS", "PLANE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PLANE_API_KEY", "test-key")
    set_active_workspace(None)
    yield
    set_active_workspace(None)


class TestConfiguredSlugs:
    def test_empty_when_nothing_configured(self):
        assert get_configured_workspace_slugs() == []

    def test_single_slug_still_works(self, monkeypatch):
        """The pre-existing single-workspace setup must keep working untouched."""
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "marketing")
        assert get_configured_workspace_slugs() == ["marketing"]

    def test_default_leads_the_list(self, monkeypatch):
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "marketing")
        monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "unity,cocos")
        assert get_configured_workspace_slugs() == ["marketing", "unity", "cocos"]

    def test_deduplicates_preserving_order(self, monkeypatch):
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "unity")
        monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "cocos,unity,marketing")
        assert get_configured_workspace_slugs() == ["unity", "cocos", "marketing"]

    def test_tolerates_whitespace_and_newlines(self, monkeypatch):
        monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", " unity , \n cocos \n")
        assert get_configured_workspace_slugs() == ["unity", "cocos"]

    def test_ignores_empty_segments(self, monkeypatch):
        monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "unity,,,cocos,")
        assert get_configured_workspace_slugs() == ["unity", "cocos"]


class TestResolutionOrder:
    def test_falls_back_to_env_default(self, monkeypatch):
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "marketing")
        assert get_plane_client_context().workspace_slug == "marketing"

    def test_session_default_beats_env(self, monkeypatch):
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "marketing")
        set_active_workspace("unity")
        assert get_plane_client_context().workspace_slug == "unity"

    def test_per_call_beats_session_default(self, monkeypatch):
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "marketing")
        set_active_workspace("unity")
        assert get_plane_client_context("cocos").workspace_slug == "cocos"

    def test_per_call_does_not_leak_into_session(self, monkeypatch):
        """A per-call override is for that call only."""
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "marketing")
        set_active_workspace("unity")
        get_plane_client_context("cocos")
        assert get_active_workspace() == "unity"
        assert get_plane_client_context().workspace_slug == "unity"

    def test_blank_override_is_ignored(self, monkeypatch):
        """An empty string must not blank out a good default."""
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "marketing")
        assert get_plane_client_context("").workspace_slug == "marketing"
        assert get_plane_client_context("   ").workspace_slug == "marketing"

    def test_override_is_trimmed(self, monkeypatch):
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "marketing")
        assert get_plane_client_context("  unity  ").workspace_slug == "unity"

    def test_clearing_session_default_restores_env(self, monkeypatch):
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "marketing")
        set_active_workspace("unity")
        set_active_workspace(None)
        assert get_plane_client_context().workspace_slug == "marketing"

    def test_blank_session_default_is_treated_as_cleared(self, monkeypatch):
        monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "marketing")
        set_active_workspace("   ")
        assert get_active_workspace() is None
        assert get_plane_client_context().workspace_slug == "marketing"


class TestSlugLessServer:
    """The server must start and stay usable with no PLANE_WORKSPACE_SLUG set.

    Requiring the slug at startup made an undiscoverable value an install-time
    blocker: Plane exposes no workspace-listing endpoint, so an operator cannot
    look it up from the API. These pin the two halves of the fix -- an
    unresolved workspace fails loudly at the call that needs it, and a call that
    genuinely does not need one still works.
    """

    def test_unresolved_workspace_raises(self):
        with pytest.raises(MissingWorkspaceError):
            get_plane_client_context()

    def test_error_names_the_ways_to_supply_one(self):
        """An unactionable error here sends the operator back to reinstalling."""
        with pytest.raises(MissingWorkspaceError) as excinfo:
            get_plane_client_context()
        message = str(excinfo.value)
        assert "workspace_slug" in message
        assert "set_workspace" in message
        assert "PLANE_WORKSPACE_SLUG" in message

    def test_empty_slug_never_reaches_the_sdk(self):
        """An empty slug builds `/workspaces//...` and 404s opaquely."""
        with pytest.raises(MissingWorkspaceError):
            get_plane_client_context("   ")

    def test_workspace_independent_call_still_works(self):
        """get_me hits /users/me/ -- it must not need a workspace."""
        ctx = get_plane_client_context(require_workspace=False)
        assert ctx.workspace_slug == ""
        assert ctx.client is not None

    def test_per_call_slug_satisfies_the_requirement(self):
        assert get_plane_client_context("unity").workspace_slug == "unity"

    def test_session_default_satisfies_the_requirement(self):
        set_active_workspace("unity")
        assert get_plane_client_context().workspace_slug == "unity"
