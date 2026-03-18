"""Schema introspection tools for DERIVA catalogs.

Provides MCP tools for browsing DERIVA catalog structure:
    get_catalog_info   -- Catalog metadata
    list_schemas       -- Schema names within a catalog
    get_schema         -- Tables, columns, keys, and foreign keys for a schema
    get_table          -- Full definition of a single table
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deriva_mcp_core.plugin.api import PluginContext


def register(ctx: PluginContext) -> None:
    """Register schema introspection tools with the MCP server."""
    # TODO (Phase 4): implement tools
