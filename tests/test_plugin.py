"""Unit tests for PluginContext lifecycle hooks and plugin loader."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from deriva_mcp_core.context import set_mutation_allowed
from deriva_mcp_core.plugin.api import (
    PluginContext,
    RagDatasetIndexerDeclaration,
    RagLocalSourceDeclaration,
    RagGitHubSourceDeclaration,
    RagWebSourceDeclaration,
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
# rag_github_source declaration
# ---------------------------------------------------------------------------


def test_rag_github_source_stored(ctx):
    ctx.rag_github_source(
        name="test-docs",
        repo_owner="my-org",
        repo_name="my-repo",
        branch="main",
        path_prefix="docs/",
    )
    assert len(ctx._rag_sources) == 1
    src = ctx._rag_sources[0]
    assert isinstance(src, RagGitHubSourceDeclaration)
    assert src.name == "test-docs"
    assert src.doc_type == "user-guide"  # default


def test_rag_github_source_custom_doc_type(ctx):
    ctx.rag_github_source(
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


# ---------------------------------------------------------------------------
# Plugin allowlist
# ---------------------------------------------------------------------------


def _make_eps(*names):
    """Return a list of mock entry points with the given names."""
    eps = []
    for name in names:
        ep = MagicMock()
        ep.name = name
        ep.value = f"{name}:register"
        ep.load.return_value = MagicMock()
        eps.append(ep)
    return eps


def test_allowlist_none_loads_all(ctx):
    """allowlist=None loads every discovered plugin (default open behavior)."""
    eps = _make_eps("plugin-a", "plugin-b")
    with patch("deriva_mcp_core.plugin.loader.entry_points", return_value=eps):
        load_plugins(ctx, allowlist=None)
    eps[0].load.return_value.assert_called_once_with(ctx)
    eps[1].load.return_value.assert_called_once_with(ctx)


def test_allowlist_filters_to_named_plugins(ctx):
    """allowlist restricts loading to the named entry points only."""
    eps = _make_eps("allowed-plugin", "blocked-plugin")
    with patch("deriva_mcp_core.plugin.loader.entry_points", return_value=eps):
        load_plugins(ctx, allowlist=["allowed-plugin"])
    eps[0].load.return_value.assert_called_once_with(ctx)
    eps[1].load.return_value.assert_not_called()


def test_allowlist_empty_loads_nothing(ctx):
    """An empty allowlist disables all external plugins."""
    eps = _make_eps("plugin-a", "plugin-b")
    with patch("deriva_mcp_core.plugin.loader.entry_points", return_value=eps):
        load_plugins(ctx, allowlist=[])
    eps[0].load.return_value.assert_not_called()
    eps[1].load.return_value.assert_not_called()


def test_allowlist_unknown_name_is_ignored(ctx):
    """A name in the allowlist that has no matching entry point is silently ignored."""
    eps = _make_eps("real-plugin")
    with patch("deriva_mcp_core.plugin.loader.entry_points", return_value=eps):
        load_plugins(ctx, allowlist=["real-plugin", "nonexistent-plugin"])
    eps[0].load.return_value.assert_called_once_with(ctx)


# ---------------------------------------------------------------------------
# Mutation claim guard
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_with_claim(mcp):
    return PluginContext(
        mcp,
        disable_mutating_tools=False,
        mutation_required_claim={"groups": ["mcp-mutators"]},
    )


async def test_mutation_claim_allowed_when_contextvar_true(ctx_with_claim):
    set_mutation_allowed(True)

    @ctx_with_claim.tool(mutates=True)
    async def write_tool():
        return "ok"

    result = await write_tool()
    assert result == "ok"


async def test_mutation_claim_denied_when_contextvar_false(ctx_with_claim):
    set_mutation_allowed(False)

    @ctx_with_claim.tool(mutates=True)
    async def write_tool():
        return "ok"

    result = await write_tool()
    assert "not permitted" in result


async def test_mutation_claim_does_not_block_read_tools(ctx_with_claim):
    set_mutation_allowed(False)

    @ctx_with_claim.tool(mutates=False)
    async def read_tool():
        return "data"

    result = await read_tool()
    assert result == "data"


async def test_killswitch_takes_precedence_over_claim(mcp):
    ctx = PluginContext(
        mcp,
        disable_mutating_tools=True,
        mutation_required_claim={"groups": ["mcp-mutators"]},
    )
    set_mutation_allowed(True)  # claim would pass, but killswitch wins

    @ctx.tool(mutates=True)
    async def write_tool():
        return "ok"

    result = await write_tool()
    assert "disabled" in result


async def test_no_claim_config_no_guard_overhead(mcp):
    ctx = PluginContext(mcp, disable_mutating_tools=False, mutation_required_claim=None)
    set_mutation_allowed(False)  # contextvar is False but claim check is not configured

    @ctx.tool(mutates=True)
    async def write_tool():
        return "ok"

    result = await write_tool()
    assert result == "ok"


def test_allowlist_skipped_plugin_logs_warning(ctx, caplog):
    """A discovered plugin not in the allowlist is logged at WARNING."""
    import logging

    eps = _make_eps("blocked-plugin")
    with patch("deriva_mcp_core.plugin.loader.entry_points", return_value=eps):
        with caplog.at_level(logging.WARNING, logger="deriva_mcp_core.plugin.loader"):
            load_plugins(ctx, allowlist=["other-plugin"])
    assert any("blocked-plugin" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# RAG source declaration methods
# ---------------------------------------------------------------------------


def test_rag_web_source_appends_declaration(ctx):
    ctx.rag_web_source(
        name="facebase-web",
        base_url="https://www.facebase.org",
        max_pages=100,
    )
    assert len(ctx._rag_web_sources) == 1
    decl = ctx._rag_web_sources[0]
    assert isinstance(decl, RagWebSourceDeclaration)
    assert decl.name == "facebase-web"
    assert decl.base_url == "https://www.facebase.org"
    assert decl.max_pages == 100
    assert decl.doc_type == "web-content"
    assert decl.allowed_domains == []
    assert decl.include_path_prefix == ""
    assert decl.rate_limit_seconds == 1.0


def test_rag_web_source_custom_params(ctx):
    ctx.rag_web_source(
        name="custom",
        base_url="https://example.com",
        max_pages=50,
        doc_type="custom-type",
        allowed_domains=["example.com", "cdn.example.com"],
        include_path_prefix="/docs",
        rate_limit_seconds=0.5,
    )
    decl = ctx._rag_web_sources[0]
    assert decl.allowed_domains == ["example.com", "cdn.example.com"]
    assert decl.include_path_prefix == "/docs"
    assert decl.rate_limit_seconds == 0.5


def test_rag_local_source_appends_declaration(ctx):
    ctx.rag_local_source(
        name="my-docs",
        path="/data/docs",
        glob="**/*.md",
        doc_type="user-guide",
    )
    assert len(ctx._rag_local_sources) == 1
    decl = ctx._rag_local_sources[0]
    assert isinstance(decl, RagLocalSourceDeclaration)
    assert decl.name == "my-docs"
    assert decl.path == "/data/docs"
    assert decl.glob == "**/*.md"
    assert decl.doc_type == "user-guide"
    assert decl.encoding == "utf-8"


def test_rag_local_source_defaults(ctx):
    ctx.rag_local_source(name="docs", path="/tmp/docs")
    decl = ctx._rag_local_sources[0]
    assert decl.glob == "**/*.md"
    assert decl.doc_type == "user-guide"
    assert decl.encoding == "utf-8"


def test_rag_dataset_indexer_appends_declaration(ctx):
    async def my_enricher(row, catalog):
        return f"# {row['Title']}"

    ctx.rag_dataset_indexer(
        schema="isa",
        table="dataset",
        enricher=my_enricher,
        doc_type="catalog-data",
        filter={"released": True},
        ttl_seconds=1800,
    )
    assert len(ctx._rag_dataset_indexers) == 1
    decl = ctx._rag_dataset_indexers[0]
    assert isinstance(decl, RagDatasetIndexerDeclaration)
    assert decl.schema == "isa"
    assert decl.table == "dataset"
    assert decl.enricher is my_enricher
    assert decl.doc_type == "catalog-data"
    assert decl.filter == {"released": True}
    assert decl.ttl_seconds == 1800


def test_rag_dataset_indexer_defaults(ctx):
    async def enricher(row, catalog):
        return ""

    ctx.rag_dataset_indexer(schema="public", table="item", enricher=enricher)
    decl = ctx._rag_dataset_indexers[0]
    assert decl.doc_type == "catalog-data"
    assert decl.filter == {}
    assert decl.ttl_seconds == 2**62
    assert decl.hostname is None
    assert decl.catalog_id is None
    assert decl.limit is None
    assert decl.auto_enrich is False


def test_rag_dataset_indexer_limit(ctx):
    async def enricher(row, catalog):
        return ""

    ctx.rag_dataset_indexer(schema="isa", table="dataset", enricher=enricher, limit=100)
    decl = ctx._rag_dataset_indexers[0]
    assert decl.limit == 100


def test_rag_dataset_indexer_scope_fields(ctx):
    async def enricher(row, catalog):
        return ""

    ctx.rag_dataset_indexer(
        schema="isa",
        table="dataset",
        enricher=enricher,
        hostname="www.facebase.org",
        catalog_id="1",
    )
    decl = ctx._rag_dataset_indexers[0]
    assert decl.hostname == "www.facebase.org"
    assert decl.catalog_id == "1"
