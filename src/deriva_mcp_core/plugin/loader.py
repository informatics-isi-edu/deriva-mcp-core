"""Plugin discovery and registration via Python entry points.

Scans the 'deriva_mcp.plugins' entry point group and calls each plugin's
register(ctx) function with the shared PluginContext.

Plugins are loaded after built-in tool modules are registered, so built-in tool
names take precedence over any conflicting plugin tool names.
"""

from __future__ import annotations

import logging

from deriva_mcp_core.plugin.api import PluginContext

logger = logging.getLogger(__name__)


def load_plugins(ctx: PluginContext) -> None:
    """Discover and register all installed deriva_mcp.plugins entry points.

    Args:
        ctx: The shared PluginContext wrapping the FastMCP instance.
    """
    # TODO (Phase 3): implement entry point discovery via importlib.metadata
