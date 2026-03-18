"""Phase 0 scaffold verification tests.

These tests verify that the package structure is correct and all modules import
cleanly. They contain no functional assertions -- that work begins in Phase 1.
"""

from __future__ import annotations


def test_package_imports() -> None:
    """All top-level modules import without error."""
    import importlib

    modules = [
        "deriva_mcp_core",
        "deriva_mcp_core.auth",
        "deriva_mcp_core.auth.exchange",
        "deriva_mcp_core.auth.introspect",
        "deriva_mcp_core.auth.token_cache",
        "deriva_mcp_core.auth.verifier",
        "deriva_mcp_core.config",
        "deriva_mcp_core.context",
        "deriva_mcp_core.plugin",
        "deriva_mcp_core.plugin.api",
        "deriva_mcp_core.plugin.loader",
        "deriva_mcp_core.server",
        "deriva_mcp_core.tools",
        "deriva_mcp_core.tools.catalog",
        "deriva_mcp_core.tools.entity",
        "deriva_mcp_core.tools.hatrac",
        "deriva_mcp_core.tools.query",
        "deriva_mcp_core.rag",
        "deriva_mcp_core.rag.config",
        "deriva_mcp_core.rag.store",
        "deriva_mcp_core.rag.chunker",
        "deriva_mcp_core.rag.crawler",
        "deriva_mcp_core.rag.docs",
        "deriva_mcp_core.rag.schema",
        "deriva_mcp_core.rag.data",
    ]
    for module in modules:
        assert importlib.import_module(module) is not None, f"failed to import {module}"


def test_dataclasses_importable() -> None:
    """Public dataclasses from the auth layer are importable."""
    from deriva_mcp_core.auth.exchange import ExchangeResult
    from deriva_mcp_core.auth.introspect import IntrospectionResult

    assert ExchangeResult is not None
    assert IntrospectionResult is not None


def test_public_api_importable() -> None:
    """Public API functions are importable from the top-level package."""
    from deriva_mcp_core import get_deriva_server, get_hatrac_store, get_request_credential

    assert get_deriva_server is not None
    assert get_hatrac_store is not None
    assert get_request_credential is not None


def test_plugin_context_importable() -> None:
    """PluginContext is importable from its module."""
    from deriva_mcp_core.plugin.api import PluginContext

    assert PluginContext is not None
