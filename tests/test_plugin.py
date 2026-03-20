"""Unit tests for PluginContext lifecycle hooks and plugin loader."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from deriva_mcp_core.plugin.api import (
    PluginContext,
    RagSourceDeclaration,
    _set_plugin_context,
    fire_catalog_connect,
    fire_schema_change,
)
from deriva_mcp_core.plugin.loader import load_plugins

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp():
    """Minimal FastMCP mock -- only needs .tool(), .resource(), .prompt()."""
    mock = MagicMock()
    mock.tool.return_value = lambda fn: fn
    mock.resource.return_value = lambda fn: fn
    mock.prompt.return_value = lambda fn: fn
    return mock


@pytest.fixture
def ctx(mcp):
    return PluginContext(mcp)


@pytest.fixture(autouse=True)
def reset_plugin_context():
    """Ensure the module-level singleton is reset after each test."""
    yield
    _set_plugin_context(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tool / resource / prompt registration
# ---------------------------------------------------------------------------


def test_tool_delegates_to_fastmcp(ctx, mcp):
    @ctx.tool(mutates=False)
    def my_tool():
        pass

    mcp.tool.assert_called_once()


def test_resource_delegates_to_fastmcp(ctx, mcp):
    @ctx.resource("deriva://test/{param}")
    def my_resource(param: str):
        pass

    mcp.resource.assert_called_once_with("deriva://test/{param}")


def test_prompt_delegates_to_fastmcp(ctx, mcp):
    @ctx.prompt("my-prompt")
    def my_prompt():
        pass

    mcp.prompt.assert_called_once_with("my-prompt")


# ---------------------------------------------------------------------------
# rag_source declaration
# ---------------------------------------------------------------------------


def test_rag_source_stored(ctx):
    ctx.rag_source(
        name="test-docs",
        repo_owner="my-org",
        repo_name="my-repo",
        branch="main",
        path_prefix="docs/",
    )
    assert len(ctx._rag_sources) == 1
    src = ctx._rag_sources[0]
    assert isinstance(src, RagSourceDeclaration)
    assert src.name == "test-docs"
    assert src.doc_type == "user-guide"  # default


def test_rag_source_custom_doc_type(ctx):
    ctx.rag_source(
        name="api-docs",
        repo_owner="org",
        repo_name="repo",
        branch="main",
        path_prefix="api/",
        doc_type="api-reference",
    )
    assert ctx._rag_sources[0].doc_type == "api-reference"


# ---------------------------------------------------------------------------
# Lifecycle hooks: on_catalog_connect
# ---------------------------------------------------------------------------


async def test_on_catalog_connect_called(ctx):
    called_with = {}

    async def my_hook(hostname, catalog_id, schema_hash, schema_json):
        called_with.update(
            hostname=hostname,
            catalog_id=catalog_id,
            schema_hash=schema_hash,
            schema_json=schema_json,
        )

    ctx.on_catalog_connect(my_hook)
    _set_plugin_context(ctx)

    fire_catalog_connect("host.example.org", "1", "abc123", {"tables": []})
    await asyncio.sleep(0)  # allow fire-and-forget tasks to run

    assert called_with["hostname"] == "host.example.org"
    assert called_with["catalog_id"] == "1"
    assert called_with["schema_hash"] == "abc123"
    assert called_with["schema_json"] == {"tables": []}


async def test_multiple_catalog_connect_hooks_all_called(ctx):
    results = []

    async def hook_a(hostname, catalog_id, schema_hash, schema_json):
        results.append("a")

    async def hook_b(hostname, catalog_id, schema_hash, schema_json):
        results.append("b")

    ctx.on_catalog_connect(hook_a)
    ctx.on_catalog_connect(hook_b)
    _set_plugin_context(ctx)

    fire_catalog_connect("h", "1", "hash", {})
    await asyncio.sleep(0)

    assert sorted(results) == ["a", "b"]


# ---------------------------------------------------------------------------
# Lifecycle hooks: on_schema_change
# ---------------------------------------------------------------------------


async def test_on_schema_change_called(ctx):
    called_with = {}

    async def my_hook(hostname, catalog_id):
        called_with.update(hostname=hostname, catalog_id=catalog_id)

    ctx.on_schema_change(my_hook)
    _set_plugin_context(ctx)

    fire_schema_change("host.example.org", "2")
    await asyncio.sleep(0)

    assert called_with["hostname"] == "host.example.org"
    assert called_with["catalog_id"] == "2"


# ---------------------------------------------------------------------------
# Hook exception isolation
# ---------------------------------------------------------------------------


async def test_hook_exception_does_not_propagate(ctx):
    """An exception in a lifecycle hook must not raise in the caller."""

    async def bad_hook(hostname, catalog_id, schema_hash, schema_json):
        raise RuntimeError("hook failure")

    async def good_hook(hostname, catalog_id, schema_hash, schema_json):
        pass  # should still be called

    ctx.on_catalog_connect(bad_hook)
    ctx.on_catalog_connect(good_hook)
    _set_plugin_context(ctx)

    # This must not raise
    fire_catalog_connect("h", "1", "hash", {})
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# No context -- fire_* are no-ops
# ---------------------------------------------------------------------------


def test_fire_without_context_is_noop():
    """fire_* functions are safe to call before the server is initialized."""
    # _ctx is None after reset_plugin_context fixture
    fire_catalog_connect("h", "1", "hash", {})
    fire_schema_change("h", "1")


# ---------------------------------------------------------------------------
# Plugin loader
# ---------------------------------------------------------------------------


def test_load_plugins_calls_register(ctx):
    """load_plugins() calls register(ctx) for each discovered entry point."""
    mock_register = MagicMock()
    mock_ep = MagicMock()
    mock_ep.name = "test-plugin"
    mock_ep.value = "test_package.plugin:register"
    mock_ep.load.return_value = mock_register

    with patch("deriva_mcp_core.plugin.loader.entry_points", return_value=[mock_ep]):
        load_plugins(ctx)

    mock_register.assert_called_once_with(ctx)


def test_load_plugins_continues_after_failure(ctx):
    """A failing plugin does not prevent subsequent plugins from loading."""
    bad_register = MagicMock(side_effect=RuntimeError("plugin broken"))
    good_register = MagicMock()

    bad_ep = MagicMock()
    bad_ep.name = "bad-plugin"
    bad_ep.value = "bad:register"
    bad_ep.load.return_value = bad_register

    good_ep = MagicMock()
    good_ep.name = "good-plugin"
    good_ep.value = "good:register"
    good_ep.load.return_value = good_register

    with patch("deriva_mcp_core.plugin.loader.entry_points", return_value=[bad_ep, good_ep]):
        load_plugins(ctx)  # must not raise

    good_register.assert_called_once_with(ctx)
