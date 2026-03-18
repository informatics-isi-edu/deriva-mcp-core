"""Attribute and aggregate query tools for DERIVA catalogs.

Provides MCP tools for ERMRest query operations:
    query_attribute   -- Attribute query returning projected columns from a path
    query_aggregate   -- Aggregate query returning computed values over a path
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deriva_mcp_core.plugin.api import PluginContext


def register(ctx: PluginContext) -> None:
    """Register query tools with the MCP server."""
    # TODO (Phase 4): implement tools
