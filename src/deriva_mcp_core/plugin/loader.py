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


def load_plugins(ctx: PluginContext, allowlist: list[str] | None = None) -> list[str]:
    """Discover and register installed deriva_mcp.plugins entry points.

    If allowlist is None, all discovered plugins are loaded (open discovery).
    If allowlist is an empty list, no plugins are loaded.
    If allowlist is a non-empty list, only plugins whose entry point name
    appears in the list are loaded; others are logged at WARNING and skipped.

    Args:
        ctx: The shared PluginContext wrapping the FastMCP instance.
        allowlist: Optional list of permitted entry point names.

    Returns:
        List of top-level package names for successfully loaded plugins
        (e.g. ["facebase_deriva_mcp_plugin"]). Used by the caller to
        configure plugin loggers at the correct level.
    """
    loaded_packages: list[str] = []
    eps = entry_points(group="deriva_mcp.plugins")
    for ep in eps:
        if allowlist is not None and ep.name not in allowlist:
            logger.warning("Plugin %r not in DERIVA_MCP_PLUGIN_ALLOWLIST, skipping", ep.name)
            continue
        try:
            register_fn = ep.load()
            register_fn(ctx)
            logger.info("Loaded plugin: %s (%s)", ep.name, ep.value)
            # Extract the top-level package name (e.g. "facebase_deriva_mcp_plugin"
            # from "facebase_deriva_mcp_plugin.plugin:register").
            top_pkg = ep.value.split(".")[0]
            loaded_packages.append(top_pkg)
        except Exception:
            logger.exception("Failed to load plugin: %s (%s)", ep.name, ep.value)
    return loaded_packages
