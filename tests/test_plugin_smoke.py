"""Plugin smoke test.

Validates the full plugin authoring contract. A plugin package registers primary
tools (read-only and mutating) and RAG components (documentation source declaration
and a lifecycle hook that performs data indexing) through a single PluginContext.

Two packaging patterns are tested:

  Unified (recommended): one register(ctx) call covers both primary tools and RAG
  components. This is the standard pattern for a plugin that ships tools and docs
  from the same repository.

  Split: two separate register functions called on the same PluginContext. This
  supports cases where tools and RAG are maintained in separate packages or repos
  and loaded via independent entry points. The contract is identical -- both
  functions receive the same ctx and their registrations are additive.

The plugin implementation here is inline rather than a separate package. It follows
the same patterns a real plugin author would use and serves as a reference
implementation alongside the plugin authoring guide.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from deriva_mcp_core.context import set_current_credential
from deriva_mcp_core.plugin.api import (
    PluginContext,
    RagGitHubSourceDeclaration,
    _set_plugin_context,
    fire_catalog_connect,
)


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class _CapturingMCP:
    """Minimal FastMCP stand-in that stores registered tools for direct invocation."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **kwargs: Any):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def resource(self, *a: Any, **kw: Any):
        return lambda fn: fn

    def prompt(self, *a: Any, **kw: Any):
        return lambda fn: fn


@pytest.fixture()
def capturing_mcp():
    return _CapturingMCP()


@pytest.fixture()
def ctx(capturing_mcp):
    _ctx = PluginContext(capturing_mcp)
    _set_plugin_context(_ctx)
    yield _ctx
    _set_plugin_context(None)  # type: ignore[arg-type]


@pytest.fixture()
def ctx_kill_switch(capturing_mcp):
    _ctx = PluginContext(capturing_mcp, disable_mutating_tools=True)
    _set_plugin_context(_ctx)
    yield _ctx
    _set_plugin_context(None)  # type: ignore[arg-type]


@pytest.fixture()
def mock_catalog():
    catalog = MagicMock()
    resp = MagicMock()
    resp.json.return_value = [{"RID": "1-AAA", "Name": "sample item"}]
    catalog.get.return_value = resp

    mock_path = MagicMock()
    mock_path.filter.return_value = mock_path
    mock_path.update.return_value = [{"RID": "1-AAA", "Description": "updated"}]
    mock_pb = MagicMock()
    mock_pb.schemas.__getitem__.return_value.tables.__getitem__.return_value = mock_path
    catalog.getPathBuilder.return_value = mock_pb
    catalog._mock_path = mock_path
    return catalog


# ---------------------------------------------------------------------------
# Reference plugin implementation
# ---------------------------------------------------------------------------
# _RAG_SOURCE_KWARGS holds the arguments that a plugin would pass to ctx.rag_github_source().
# Defined once here so both the unified and split implementations stay in sync.

_RAG_SOURCE_KWARGS: dict[str, str] = dict(
    name="my-plugin-docs",
    repo_owner="my-org",
    repo_name="my-plugin",
    branch="main",
    path_prefix="docs/",
    doc_type="user-guide",
)


def _register_unified(ctx: PluginContext, index_fn=None) -> None:
    """Unified register function -- primary tools and RAG components together.

    This is the recommended pattern for a plugin that ships tools and documentation
    from the same repository. A single register(ctx) entry point handles both.
    index_fn stands in for an index_table_data() call in a real plugin.

    Note: imports are inside each tool function body (not at the top of register()).
    This is the correct pattern -- it ensures the import resolves at call time so
    test patches applied after registration take effect.
    """

    @ctx.tool(mutates=False)
    async def plugin_get_items(hostname: str, catalog_id: str) -> str:
        from deriva_mcp_core import deriva_call, get_catalog
        with deriva_call():
            catalog = get_catalog(hostname, catalog_id)
            rows = catalog.get("/entity/public:Item").json()
        return json.dumps({"rows": rows})

    @ctx.tool(mutates=True)
    async def plugin_set_description(
        hostname: str, catalog_id: str, rid: str, description: str
    ) -> str:
        from deriva_mcp_core import deriva_call, get_catalog
        with deriva_call():
            catalog = get_catalog(hostname, catalog_id)
            pb = catalog.getPathBuilder()
            path = pb.schemas["public"].tables["Item"]
            path.filter(path.RID == rid).update([{"RID": rid, "Description": description}])
        return json.dumps({"updated": True})

    # RAG component: data-indexing lifecycle hook.
    # A real plugin would call index_table_data() here to index domain-specific rows.
    async def _on_connect(hostname: str, catalog_id: str, schema_hash: str, schema_json: dict) -> None:
        if index_fn is not None:
            index_fn(hostname, catalog_id)

    ctx.on_catalog_connect(_on_connect)

    # RAG component: documentation source declaration.
    ctx.rag_github_source(**_RAG_SOURCE_KWARGS)


def _register_tools_only(ctx: PluginContext) -> None:
    """Tools-only register -- the primary-tool half of the split-package pattern."""

    @ctx.tool(mutates=False)
    async def plugin_get_items(hostname: str, catalog_id: str) -> str:
        from deriva_mcp_core import deriva_call, get_catalog
        with deriva_call():
            catalog = get_catalog(hostname, catalog_id)
            rows = catalog.get("/entity/public:Item").json()
        return json.dumps({"rows": rows})

    @ctx.tool(mutates=True)
    async def plugin_set_description(
        hostname: str, catalog_id: str, rid: str, description: str
    ) -> str:
        from deriva_mcp_core import deriva_call, get_catalog
        with deriva_call():
            catalog = get_catalog(hostname, catalog_id)
            pb = catalog.getPathBuilder()
            path = pb.schemas["public"].tables["Item"]
            path.filter(path.RID == rid).update([{"RID": rid, "Description": description}])
        return json.dumps({"updated": True})


def _register_rag_only(ctx: PluginContext, index_fn=None) -> None:
    """RAG-only register -- the RAG half of the split-package pattern.

    A separate package (separate repo, separate entry point) that adds a
    documentation source and a data-indexing lifecycle hook to the same
    PluginContext that _register_tools_only already populated.
    """

    async def _on_connect(hostname: str, catalog_id: str, schema_hash: str, schema_json: dict) -> None:
        if index_fn is not None:
            index_fn(hostname, catalog_id)

    ctx.on_catalog_connect(_on_connect)
    ctx.rag_github_source(**_RAG_SOURCE_KWARGS)


# ---------------------------------------------------------------------------
# Registration contract
# ---------------------------------------------------------------------------


def test_unified_registers_both_tools(ctx):
    _register_unified(ctx)
    assert "plugin_get_items" in ctx._mcp.tools
    assert "plugin_set_description" in ctx._mcp.tools


def test_unified_registers_rag_github_source(ctx):
    _register_unified(ctx)
    assert len(ctx._rag_sources) == 1
    src = ctx._rag_sources[0]
    assert isinstance(src, RagGitHubSourceDeclaration)
    assert src.name == "my-plugin-docs"
    assert src.repo_owner == "my-org"
    assert src.doc_type == "user-guide"


def test_unified_registers_catalog_connect_hook(ctx):
    _register_unified(ctx)
    assert len(ctx._catalog_connect_hooks) == 1


def test_split_pattern_registers_same_tools_and_rag(ctx):
    """Two separate register functions on the same ctx produce the same state
    as the unified pattern."""
    index_fn = MagicMock()
    _register_tools_only(ctx)
    _register_rag_only(ctx, index_fn=index_fn)

    assert "plugin_get_items" in ctx._mcp.tools
    assert "plugin_set_description" in ctx._mcp.tools
    assert len(ctx._rag_sources) == 1
    assert ctx._rag_sources[0].name == "my-plugin-docs"
    assert len(ctx._catalog_connect_hooks) == 1


def test_mutates_required(ctx):
    """Omitting mutates= raises TypeError at registration time."""
    with pytest.raises(TypeError, match="mutates="):

        @ctx.tool()
        async def bad_tool() -> str:
            return ""


# ---------------------------------------------------------------------------
# Primary tool execution
# ---------------------------------------------------------------------------


async def test_read_tool_executes(ctx, mock_catalog):
    set_current_credential({"bearer-token": "test-derived-token"})
    _register_unified(ctx)
    with patch("deriva_mcp_core.get_catalog", return_value=mock_catalog):
        result = json.loads(await ctx._mcp.tools["plugin_get_items"]("host.example.org", "1"))
    assert result["rows"] == [{"RID": "1-AAA", "Name": "sample item"}]


async def test_write_tool_executes(ctx, mock_catalog):
    set_current_credential({"bearer-token": "test-derived-token"})
    _register_unified(ctx)
    with patch("deriva_mcp_core.get_catalog", return_value=mock_catalog):
        result = json.loads(
            await ctx._mcp.tools["plugin_set_description"](
                "host.example.org", "1", "1-AAA", "new description"
            )
        )
    assert result["updated"] is True
    mock_catalog._mock_path.update.assert_called_once()


async def test_write_tool_blocked_by_kill_switch(ctx_kill_switch, mock_catalog):
    """With disable_mutating_tools=True the write tool returns the error payload
    without touching the catalog."""
    _register_unified(ctx_kill_switch)
    with patch("deriva_mcp_core.get_catalog", return_value=mock_catalog):
        result = json.loads(
            await ctx_kill_switch._mcp.tools["plugin_set_description"](
                "host.example.org", "1", "1-AAA", "blocked"
            )
        )
    assert "error" in result
    mock_catalog.getPathBuilder.assert_not_called()


# ---------------------------------------------------------------------------
# RAG lifecycle hook -- data indexing component
# ---------------------------------------------------------------------------


async def test_catalog_connect_hook_fires_with_correct_args(ctx):
    """on_catalog_connect hook receives hostname, catalog_id, hash, and schema."""
    index_fn = MagicMock()
    _register_unified(ctx, index_fn=index_fn)

    fire_catalog_connect("host.example.org", "42", "deadbeef", {"schemas": {}})
    await asyncio.sleep(0)  # allow fire-and-forget task to run

    index_fn.assert_called_once_with("host.example.org", "42")


async def test_split_catalog_connect_hook_fires(ctx):
    """Split-package hook fires the same way as the unified hook."""
    index_fn = MagicMock()
    _register_tools_only(ctx)
    _register_rag_only(ctx, index_fn=index_fn)

    fire_catalog_connect("host.example.org", "7", "cafebabe", {})
    await asyncio.sleep(0)

    index_fn.assert_called_once_with("host.example.org", "7")


async def test_hook_exception_does_not_surface_to_caller(ctx):
    """A failing hook must not cause fire_catalog_connect to raise."""

    async def _bad_hook(hostname, catalog_id, schema_hash, schema_json):
        raise RuntimeError("indexing exploded")

    ctx.on_catalog_connect(_bad_hook)
    _set_plugin_context(ctx)

    fire_catalog_connect("h", "1", "hash", {})
    await asyncio.sleep(0)  # must not raise