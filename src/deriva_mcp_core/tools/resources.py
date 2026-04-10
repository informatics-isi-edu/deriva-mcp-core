"""Built-in MCP resources for DERIVA catalog and server introspection.

Resources are read-accessible URIs that MCP clients can fetch directly via
read_resource() without a tool call. They are suitable for stable, reference-
like data such as schema definitions and server configuration.

Available resources:

    deriva://server/status
        Server version, auth mode, enabled features, RAG configuration.
        Static after startup (reflects state at time of first request).

    deriva://catalog/{hostname}/{catalog_id}/schema
        Full ERMrest schema JSON for the catalog. Served from the in-process
        schema cache when warm (populated by any prior catalog tool call for
        the same hostname/catalog_id); falls back to a live ERMrest fetch.

    deriva://catalog/{hostname}/{catalog_id}/tables
        Flat list of {schema, table, comment} for every non-system table.
        Derived from the same cached or fetched schema.

    deriva://catalog/{hostname}/{catalog_id}/table/{schema}/{table}
        Full ERMrest table definition (columns, foreign keys, annotations).
        Derived from the same cached or fetched schema.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
from typing import TYPE_CHECKING

from ..config import settings as _settings
from ..context import deriva_call, get_catalog
from ..rag import get_rag_status, get_rag_store
from .catalog import _SYSTEM_SCHEMAS, get_cached_schema

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)


def _server_status() -> dict:
    """Build the server status payload. Called at request time so RAG state is current."""
    try:
        version = importlib.metadata.version("deriva-mcp-core")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    rag_status = get_rag_status()
    return {
        "version": version,
        "auth_mode": "http" if _settings.credenza_url else "stdio",
        "allow_anonymous": _settings.allow_anonymous,
        "mutating_tools_enabled": not _settings.disable_mutating_tools,
        "rag_enabled": get_rag_store() is not None,
        "rag": rag_status,
        "server_url": _settings.server_url or None,
    }


def _get_schema(hostname: str, catalog_id: str) -> dict:
    """Return schema JSON from cache, or fetch live and cache it."""
    cached = get_cached_schema(hostname, catalog_id)
    if cached is not None:
        return cached
    with deriva_call():
        catalog = get_catalog(hostname, catalog_id)
        schema_json = catalog.get("/schema").json()
    return schema_json


def register(ctx: PluginContext) -> None:
    """Register built-in deriva:// resources."""

    @ctx.resource("deriva://server/status")
    async def server_status() -> str:
        return json.dumps(_server_status())

    @ctx.resource("deriva://catalog/{hostname}/{catalog_id}/schema")
    async def catalog_schema(hostname: str, catalog_id: str) -> str:
        return json.dumps(_get_schema(hostname, catalog_id))

    @ctx.resource("deriva://catalog/{hostname}/{catalog_id}/tables")
    async def catalog_tables(hostname: str, catalog_id: str) -> str:
        schema_json = _get_schema(hostname, catalog_id)
        tables = [
            {"schema": sname, "table": tname, "comment": tdef.get("comment")}
            for sname, sdef in schema_json.get("schemas", {}).items()
            if sname not in _SYSTEM_SCHEMAS
            for tname, tdef in sdef.get("tables", {}).items()
        ]
        return json.dumps({"tables": tables})

    @ctx.resource("deriva://catalog/{hostname}/{catalog_id}/table/{schema}/{table}")
    async def catalog_table(hostname: str, catalog_id: str, schema: str, table: str) -> str:
        schema_json = _get_schema(hostname, catalog_id)
        schema_doc = schema_json.get("schemas", {}).get(schema)
        if schema_doc is None:
            return json.dumps({"error": f"Schema not found: {schema!r}"})
        tdef = schema_doc.get("tables", {}).get(table)
        if tdef is None:
            return json.dumps({"error": f"Table not found: {schema!r}:{table!r}"})
        return json.dumps(tdef)