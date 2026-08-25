"""
On-demand PQL syntax reference.
"""

from typing import Literal

from fastmcp import FastMCP

from plane_mcp.pql_support import cached_pql_support
from plane_mcp.tools.pql_reference import PQL_FIELD_DESCRIPTION, PQL_FULL_REFERENCE


def register_pql_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_pql_reference(detail: Literal["brief", "full"] = "full") -> dict:
        """
        Return the Plane Query Language (PQL) syntax reference.

        Call this when composing the `pql` filter for `list_work_items`,
        `list_archived_work_items`, `list_cycle_work_items`, `list_module_work_items`,
        or `count_work_items`.

        Args:
            detail: "full" (default) returns the comprehensive reference with
                all operators, functions, common mistakes, and worked examples.
                "brief" returns the compact field/operator/function quick
                reference (lighter payload for simple queries).

        Returns:
            Dict with `detail` (which version was returned), `reference` (the
            PQL syntax text), and `pql_supported` — False once this deployment
            has been observed ignoring the `pql` parameter, None while that is
            still unknown. False means the language below is documented but not
            implemented here: filter client-side instead.
        """
        reference = PQL_FIELD_DESCRIPTION if detail == "brief" else PQL_FULL_REFERENCE
        return {
            "detail": detail,
            "reference": reference,
            "pql_supported": cached_pql_support(),
        }
