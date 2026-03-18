"""Entity CRUD tools for DERIVA catalogs.

Provides MCP tools for ERMRest entity operations:
    get_entities     -- Retrieve entities from a table (with optional filters)
    insert_entities  -- Insert new entity records (POST)
    update_entities  -- Update existing entity records (PUT)
    delete_entities  -- Delete entity records matching filters (DELETE)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deriva_mcp_core.plugin.api import PluginContext


def register(ctx: PluginContext) -> None:
    """Register entity CRUD tools with the MCP server."""
    # TODO (Phase 4): implement tools
