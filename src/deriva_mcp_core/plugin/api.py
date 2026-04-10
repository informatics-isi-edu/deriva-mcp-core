from __future__ import annotations

"""PluginContext -- the registration API passed to each plugin's register() function.

A plugin's register() function receives a PluginContext instance and uses it to
register tools, resources, prompts, lifecycle hooks, and RAG documentation sources.

Plugin entry point contract:

    def register(ctx: PluginContext) -> None:
        @ctx.tool(mutates=False)
        async def my_tool(hostname: str, catalog_id: str) -> str:
            from deriva_mcp_core import get_catalog
            catalog = get_catalog(hostname, catalog_id)
            ...

        ctx.on_catalog_connect(my_schema_hook)
        ctx.rag_github_source(
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

    ctx.rag_github_source() is a no-op when DERIVA_MCP_RAG_ENABLED=false, so plugins need
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

from ..context import get_request_bearer_token, get_request_user_id, is_mutation_allowed
from ..telemetry import audit_event

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..tasks.manager import TaskManager

logger = logging.getLogger(__name__)

# Server-level singleton set once at startup by server.py.
# Safe to read from any coroutine after startup; never mutated after that.
_ctx: PluginContext | None = None

# Holds references to background hook tasks to prevent premature GC.
_background_tasks: set[asyncio.Task[None]] = set()


@dataclass
class RagGitHubSourceDeclaration:
    """A GitHub documentation source declared by a plugin via ctx.rag_github_source()."""

    name: str
    repo_owner: str
    repo_name: str
    branch: str
    path_prefix: str
    doc_type: str


@dataclass
class RagWebSourceDeclaration:
    """A website crawl source declared by a plugin via ctx.rag_web_source()."""

    name: str
    base_url: str
    max_pages: int
    doc_type: str
    allowed_domains: list[str]
    include_path_prefix: str
    rate_limit_seconds: float


@dataclass
class RagLocalSourceDeclaration:
    """A local filesystem source declared by a plugin via ctx.rag_local_source()."""

    name: str
    path: str
    glob: str
    doc_type: str
    encoding: str


@dataclass
class RagDatasetIndexerDeclaration:
    """A dataset enrichment hook declared by a plugin via ctx.rag_dataset_indexer()."""

    schema: str
    table: str
    enricher: Any  # async callable: (row: dict, catalog) -> str
    doc_type: str
    filter: dict[str, Any]
    ttl_seconds: int
    hostname: str | None  # if set, only run when connected catalog hostname matches
    catalog_id: str | None  # if set, only run when connected catalog_id matches
    limit: int | None  # if set, appended as ?limit=N to the ERMrest fetch URL


_UNSET = object()
_MUTATIONS_DISABLED_RESPONSE = json.dumps(
    {"error": "catalog mutations are disabled by server configuration"}
)
_MUTATIONS_NOT_PERMITTED_RESPONSE = json.dumps(
    {"error": "catalog mutations are not permitted for your account"}
)


class PluginContext:
    """Wraps a FastMCP instance and exposes the full plugin registration API."""

    def __init__(
        self,
        mcp: FastMCP,
        disable_mutating_tools: bool = False,
        mutation_required_claim: dict[str, Any] | None = None,
        task_manager: TaskManager | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._mcp = mcp
        self._disable_mutating_tools = disable_mutating_tools
        self._mutation_required_claim = mutation_required_claim
        self._task_manager = task_manager
        self.env: dict[str, str] = env or {}
        """Merged environment: env file values overlaid by os.environ (OS wins).
        Plugins read their configuration from here rather than accessing os.environ
        or an env file path directly, so deployment-time overrides are always
        respected regardless of how the server was started."""
        self._catalog_connect_hooks: list[Callable[..., Any]] = []
        self._schema_change_hooks: list[Callable[..., Any]] = []
        self._rag_sources: list[RagGitHubSourceDeclaration] = []
        self._rag_web_sources: list[RagWebSourceDeclaration] = []
        self._rag_local_sources: list[RagLocalSourceDeclaration] = []
        self._rag_dataset_indexers: list[RagDatasetIndexerDeclaration] = []

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

        needs_guard = mutates and (
            self._disable_mutating_tools or self._mutation_required_claim is not None
        )
        if not needs_guard:
            return mcp_decorator

        # mutates=True and at least one guard is active -- wrap with per-call check
        disable = self._disable_mutating_tools
        claim_spec = self._mutation_required_claim

        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            async def guarded(*a: Any, **kw: Any) -> Any:
                if disable:
                    return _MUTATIONS_DISABLED_RESPONSE
                if claim_spec is not None and not is_mutation_allowed():
                    audit_event("mutation_claim_denied")
                    return _MUTATIONS_NOT_PERMITTED_RESPONSE
                return await fn(*a, **kw)

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
    # Background task submission
    # ------------------------------------------------------------------

    def submit_task(
        self,
        coroutine: Any,
        name: str,
        description: str = "",
    ) -> str:
        """Submit a coroutine as a background task and return its task_id immediately.

        Must be called from within a tool handler (async context). Captures the
        current principal and bearer token from contextvars so the task can
        re-exchange credentials if needed.

        Args:
            coroutine: Awaitable to run as the task body.
            name: Short human-readable task name.
            description: Optional longer description.

        Returns:
            task_id string (UUID4). Pass to get_task_status / cancel_task.

        Raises:
            RuntimeError: If the TaskManager was not injected at startup.
        """
        if self._task_manager is None:
            raise RuntimeError(
                "TaskManager not configured. "
                "Ensure create_server() initializes the task manager before plugins load."
            )
        principal = get_request_user_id()
        bearer_token = get_request_bearer_token()
        return self._task_manager.submit(
            coroutine,
            name=name,
            principal=principal,
            bearer_token=bearer_token,
            description=description,
        )

    # ------------------------------------------------------------------
    # RAG source declaration
    # ------------------------------------------------------------------

    def rag_github_source(
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
            RagGitHubSourceDeclaration(
                name=name,
                repo_owner=repo_owner,
                repo_name=repo_name,
                branch=branch,
                path_prefix=path_prefix,
                doc_type=doc_type,
            )
        )

    def rag_web_source(
        self,
        name: str,
        base_url: str,
        max_pages: int = 200,
        doc_type: str = "web-content",
        allowed_domains: list[str] | None = None,
        include_path_prefix: str = "",
        rate_limit_seconds: float = 1.0,
    ) -> None:
        """Declare a website crawl source for the RAG subsystem.

        The crawler performs async BFS from base_url up to max_pages, extracts
        text content from HTML, deduplicates by content hash, and upserts into
        the vector store. Is a no-op when DERIVA_MCP_RAG_ENABLED=false.

        Args:
            name: Unique source identifier.
            base_url: Crawl root URL.
            max_pages: Maximum pages to collect.
            doc_type: Document type tag stored in the vector store.
            allowed_domains: Domains whose links may be followed. Defaults to
                the domain of base_url.
            include_path_prefix: Only index URLs under this path prefix.
            rate_limit_seconds: Delay between HTTP requests.
        """
        self._rag_web_sources.append(
            RagWebSourceDeclaration(
                name=name,
                base_url=base_url,
                max_pages=max_pages,
                doc_type=doc_type,
                allowed_domains=allowed_domains or [],
                include_path_prefix=include_path_prefix,
                rate_limit_seconds=rate_limit_seconds,
            )
        )

    def rag_local_source(
        self,
        name: str,
        path: str,
        glob: str = "**/*.md",
        doc_type: str = "user-guide",
        encoding: str = "utf-8",
    ) -> None:
        """Declare a local filesystem documentation source for the RAG subsystem.

        Walks path using the glob pattern, reads each matching file, chunks it
        with chunk_markdown(), and upserts into the vector store. Change
        detection uses file mtime. Is a no-op when DERIVA_MCP_RAG_ENABLED=false.

        Args:
            name: Unique source identifier.
            path: Absolute or relative path to a directory or single file.
            glob: Glob pattern for file discovery (default: **/*.md).
            doc_type: Document type tag stored in the vector store.
            encoding: File encoding (default: utf-8).
        """
        self._rag_local_sources.append(
            RagLocalSourceDeclaration(
                name=name,
                path=path,
                glob=glob,
                doc_type=doc_type,
                encoding=encoding,
            )
        )

    def rag_dataset_indexer(
        self,
        schema: str,
        table: str,
        enricher: Any,
        doc_type: str = "catalog-data",
        filter: dict[str, Any] | None = None,  # noqa: A002
        ttl_seconds: int = 2**62,
        hostname: str | None = None,
        catalog_id: str | None = None,
        limit: int | None = None,
    ) -> None:
        """Register a dataset enrichment hook for the RAG subsystem.

        At catalog connect time, the framework fetches filtered rows from
        schema:table, calls enricher(row, catalog) -> str for each row, chunks
        the result, and upserts into the vector store scoped to the catalog.

        The enricher is called only if the source has not been indexed within
        ttl_seconds. Staleness is checked per (hostname, catalog_id, schema, table).

        Is a no-op when DERIVA_MCP_RAG_ENABLED=false.

        Args:
            schema: ERMrest schema name.
            table: ERMrest table name.
            enricher: Async callable (row: dict, catalog) -> str producing
                Markdown for one row.
            doc_type: Document type tag stored in the vector store.
            filter: Optional dict of column=value filters applied to the
                ERMrest query (e.g., {"released": True}).
            ttl_seconds: Skip re-indexing if the source was indexed within
                this many seconds (default 3600).
            hostname: If set, only run this enricher when the connecting catalog
                hostname matches. Use this when the enricher is specific to one
                server (e.g. a FaceBase-only indexer should not fire on other catalogs).
            catalog_id: If set, only run this enricher when the connecting
                catalog_id matches.
        """
        self._rag_dataset_indexers.append(
            RagDatasetIndexerDeclaration(
                schema=schema,
                table=table,
                enricher=enricher,
                doc_type=doc_type,
                filter=filter or {},
                ttl_seconds=ttl_seconds,
                hostname=hostname,
                catalog_id=catalog_id,
                limit=limit,
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
