from __future__ import annotations

"""Plugin discovery and registration via Python entry points.

Scans the 'deriva_mcp.plugins' entry point group and calls each plugin's
register(ctx) function with the shared PluginContext.

Plugins are loaded after built-in tool modules are registered, so built-in tool
names take precedence over any conflicting plugin tool names.
"""

import logging
from importlib.metadata import entry_points
from .api import PluginContext

logger = logging.getLogger(__name__)


def load_plugins(ctx: PluginContext) -> None:
    """Discover and register all installed deriva_mcp.plugins entry points.

    Args:
        ctx: The shared PluginContext wrapping the FastMCP instance.
    """
    eps = entry_points(group="deriva_mcp.plugins")
    for ep in eps:
        try:
            register_fn = ep.load()
            register_fn(ctx)
            logger.info("Loaded plugin: %s (%s)", ep.name, ep.value)
        except Exception:
            logger.exception("Failed to load plugin: %s (%s)", ep.name, ep.value)
