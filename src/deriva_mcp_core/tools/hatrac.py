"""Hatrac object store tools for DERIVA.

Provides MCP tools for basic Hatrac namespace and object operations:
    list_namespace        -- List objects and namespaces under a path
    get_object_metadata   -- Retrieve object metadata (not content)
    create_namespace      -- Create a new Hatrac namespace
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deriva_mcp_core.plugin.api import PluginContext


def register(ctx: PluginContext) -> None:
    """Register Hatrac object store tools with the MCP server."""
    # TODO (Phase 4): implement tools
