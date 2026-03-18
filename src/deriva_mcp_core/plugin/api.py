"""PluginContext -- the registration API passed to each plugin's register() function.

A plugin's register() function receives a PluginContext instance and uses it to
register tools, resources, and prompts. Handlers registered via PluginContext have
access to per-request DERIVA credentials through get_catalog() (from deriva_mcp_core).

Plugin entry point contract:
    def register(ctx: PluginContext) -> None:
        @ctx.tool()
        async def my_tool(hostname: str, catalog_id: str, ...) -> str:
            from deriva_mcp_core import get_catalog
            catalog = get_catalog(hostname, catalog_id)
            ...

        @ctx.resource("deriva://my-resource/{param}")
        async def my_resource(param: str) -> str:
            ...

        @ctx.prompt("my-prompt")
        def my_prompt() -> list:
            ...
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


class PluginContext:
    """Wraps a FastMCP instance and exposes tool/resource/prompt registration."""

    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp

    def tool(self, *args: Any, **kwargs: Any) -> Callable:
        """Decorator to register an MCP tool. Arguments forwarded to FastMCP.tool()."""
        return self._mcp.tool(*args, **kwargs)

    def resource(self, uri_pattern: str, *args: Any, **kwargs: Any) -> Callable:
        """Decorator to register an MCP resource. Arguments forwarded to FastMCP.resource()."""
        return self._mcp.resource(uri_pattern, *args, **kwargs)

    def prompt(self, name: str, *args: Any, **kwargs: Any) -> Callable:
        """Decorator to register an MCP prompt. Arguments forwarded to FastMCP.prompt()."""
        return self._mcp.prompt(name, *args, **kwargs)
