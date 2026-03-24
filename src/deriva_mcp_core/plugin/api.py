from __future__ import annotations

"""PluginContext -- the registration API passed to each plugin's register() function.

A plugin's register() function receives a PluginContext instance and uses it to
register tools, resources, prompts, lifecycle hooks, and RAG documentation sources.

Plugin entry point contract:

    def register(ctx: PluginContext) -> None:
        @ctx.tool(mutates=False)
        async def my_tool(hostname: str, catalog_id: str) -> str:
            from deriva_mcp_core import get_deriva_server
            server = get_deriva_server(hostname)
            ...

        ctx.on_catalog_connect(my_schema_hook)
        ctx.rag_source(
            name="my-plugin-docs",
            repo_owner="my-org",
            repo_name="my-repo",
            branch="main",
            path_prefix="docs/",
        )

Lifecycle hooks:
    on_catalog_connect  -- fired after any tool fetches a catalog schema;
                           callback(hostname, catalog_id, schema_hash, schema_json)
    on_schema_change    -- fired after any tool mutates a catalog schema;
                           callback(hostname, catalog_id)

    Hooks are dispatched fire-and-forget (asyncio.create_task). Exceptions in hooks
    are logged and suppressed -- a failing hook must never cause a tool call to fail.

    ctx.rag_source() is a no-op when DERIVA_MCP_RAG_ENABLED=false, so plugins need
    no RAG guard logic in their register() function.

Module-level dispatch functions (called by built-in tool implementations):
    fire_catalog_connect(hostname, catalog_id, schema_hash, schema_json)
    fire_schema_change(hostname, catalog_id)
"""


import asyncio
import functools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Server-level singleton set once at startup by server.py.
# Safe to read from any coroutine after startup; never mutated after that.
_ctx: PluginContext | None = None

# Holds references to background hook tasks to prevent premature GC.
_background_tasks: set[asyncio.Task[None]] = set()


@dataclass
class RagSourceDeclaration:
    """A documentation source declared by a plugin via ctx.rag_source()."""

    name: str
    repo_owner: str
    repo_name: str
    branch: str
    path_prefix: str
    doc_type: str


_UNSET = object()
_MUTATIONS_DISABLED_RESPONSE = json.dumps(
    {"error": "catalog mutations are disabled by server configuration"}
)


class PluginContext:
    """Wraps a FastMCP instance and exposes the full plugin registration API."""

    def __init__(self, mcp: FastMCP, disable_mutating_tools: bool = False) -> None:
        self._mcp = mcp
        self._disable_mutating_tools = disable_mutating_tools
        self._catalog_connect_hooks: list[Callable[..., Any]] = []
        self._schema_change_hooks: list[Callable[..., Any]] = []
        self._rag_sources: list[RagSourceDeclaration] = []

    # ------------------------------------------------------------------
    # MCP registration decorators -- delegate directly to FastMCP
    # ------------------------------------------------------------------

    def tool(self, *args: Any, mutates: Any = _UNSET, **kwargs: Any) -> Callable:
        """Decorator to register an MCP tool.

        mutates= is required. Pass mutates=True for tools that write to the DERIVA
        catalog or Hatrac object store; mutates=False for read-only tools. Omitting
        it raises TypeError at registration time so undeclared tools are caught at
        server startup rather than in production.

        When DERIVA_MCP_DISABLE_MUTATING_TOOLS=true, tools registered with
        mutates=True return an error response without executing.

        All other arguments are forwarded to FastMCP.tool().
        """
        if mutates is _UNSET:
            raise TypeError(
                "ctx.tool() requires a mutates= keyword argument. "
                "Pass mutates=True for tools that write catalog or object-store data, "
                "mutates=False for read-only tools."
            )
        if not isinstance(mutates, bool):
            raise TypeError("mutates= must be True or False")

        mcp_decorator = self._mcp.tool(*args, **kwargs)

        if not (mutates and self._disable_mutating_tools):
            return mcp_decorator

        # mutates=True and kill switch enabled -- wrap with guard
        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            async def guarded(*a: Any, **kw: Any) -> Any:
                return _MUTATIONS_DISABLED_RESPONSE

            return mcp_decorator(guarded)

        return decorator

    def resource(self, uri_pattern: str, *args: Any, **kwargs: Any) -> Callable:
        """Decorator to register an MCP resource. Arguments forwarded to FastMCP.resource()."""
        return self._mcp.resource(uri_pattern, *args, **kwargs)

    def prompt(self, name: str, *args: Any, **kwargs: Any) -> Callable:
        """Decorator to register an MCP prompt. Arguments forwarded to FastMCP.prompt()."""
        return self._mcp.prompt(name, *args, **kwargs)

    # ------------------------------------------------------------------
    # Lifecycle hook registration
    # ------------------------------------------------------------------

    def on_catalog_connect(self, callback: Callable[..., Any]) -> None:
        """Register a hook called after any tool connects to a catalog.

        callback signature: async (hostname, catalog_id, schema_hash, schema_json) -> None
        """
        self._catalog_connect_hooks.append(callback)

    def on_schema_change(self, callback: Callable[..., Any]) -> None:
        """Register a hook called after any tool mutates a catalog schema.

        callback signature: async (hostname, catalog_id) -> None
        """
        self._schema_change_hooks.append(callback)

    # ------------------------------------------------------------------
    # RAG source declaration
    # ------------------------------------------------------------------

    def rag_source(
        self,
        name: str,
        repo_owner: str,
        repo_name: str,
        branch: str,
        path_prefix: str,
        doc_type: str = "user-guide",
    ) -> None:
        """Declare a GitHub documentation source for the RAG subsystem.

        Called from register(ctx) so sources are discovered automatically when
        the plugin is installed. Is a no-op when DERIVA_MCP_RAG_ENABLED=false.
        """
        self._rag_sources.append(
            RagSourceDeclaration(
                name=name,
                repo_owner=repo_owner,
                repo_name=repo_name,
                branch=branch,
                path_prefix=path_prefix,
                doc_type=doc_type,
            )
        )

    # ------------------------------------------------------------------
    # Internal dispatch -- called by fire_* module-level functions
    # ------------------------------------------------------------------

    def _dispatch_catalog_connect(
        self,
        hostname: str,
        catalog_id: str,
        schema_hash: str,
        schema_json: dict,
    ) -> None:
        for hook in self._catalog_connect_hooks:
            task = asyncio.create_task(
                _safe_call(hook, hostname, catalog_id, schema_hash, schema_json)
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    def _dispatch_schema_change(self, hostname: str, catalog_id: str) -> None:
        for hook in self._schema_change_hooks:
            task = asyncio.create_task(_safe_call(hook, hostname, catalog_id))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


async def _safe_call(fn: Callable[..., Any], *args: Any) -> None:
    """Call an async hook, logging and suppressing any exception."""
    try:
        await fn(*args)
    except Exception:
        logger.exception("Exception in plugin lifecycle hook %r", getattr(fn, "__name__", fn))


def _set_plugin_context(ctx: PluginContext | None) -> None:
    """Set the server-level singleton. Called once from server.py at startup."""
    global _ctx
    _ctx = ctx


def fire_catalog_connect(
    hostname: str,
    catalog_id: str,
    schema_hash: str,
    schema_json: dict,
) -> None:
    """Dispatch on_catalog_connect hooks. Called by built-in catalog tools."""
    if _ctx is not None:
        _ctx._dispatch_catalog_connect(hostname, catalog_id, schema_hash, schema_json)


def fire_schema_change(hostname: str, catalog_id: str) -> None:
    """Dispatch on_schema_change hooks. Called by built-in schema-mutating tools."""
    if _ctx is not None:
        _ctx._dispatch_schema_change(hostname, catalog_id)
