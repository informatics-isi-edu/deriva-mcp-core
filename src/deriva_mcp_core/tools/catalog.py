from __future__ import annotations

"""Schema introspection tools for DERIVA catalogs.

Provides MCP tools for browsing DERIVA catalog structure:
    get_catalog_info   -- Catalog metadata: schema names and table counts
    list_schemas       -- Schema names within a catalog
    get_schema         -- Tables and columns for a schema
    get_table          -- Full definition of a single table
"""

import asyncio
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from ..context import _remap, _set_catalog_access_fn, deriva_call, get_catalog
from ..plugin.api import fire_catalog_connect

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)

# System-managed schemas hidden from user-visible listings
_SYSTEM_SCHEMAS: frozenset[str] = frozenset({"_ermrest", "_acl_admin"})


def _compute_schema_hash(schema_json: dict) -> str:
    return hashlib.sha256(json.dumps(schema_json, sort_keys=True).encode()).hexdigest()


def _col_summary(col: dict) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": col["name"],
        "type": col.get("type", {}).get("typename", "unknown"),
        "nullok": col.get("nullok", True),
    }
    comment = col.get("comment")
    if comment:
        info["comment"] = comment
    return info


def _fk_summary(fk: dict) -> dict[str, Any]:
    fk_cols = [c["column_name"] for c in fk.get("foreign_key_columns", [])]
    ref_cols = fk.get("referenced_columns", [])
    ref_table = f"{ref_cols[0]['schema_name']}:{ref_cols[0]['table_name']}" if ref_cols else None
    return {
        "columns": fk_cols,
        "references": ref_table,
        "referenced_columns": [c["column_name"] for c in ref_cols],
    }


def _fetch_schema(hostname: str, catalog_id: str) -> dict:
    """Fetch full schema JSON, compute hash, and fire on_catalog_connect hooks."""
    # Pre-claim the slot so _on_catalog_access (triggered by get_catalog below)
    # does not schedule a redundant background _fetch_schema for the same catalog.
    _connected_catalogs.add((_remap(hostname), catalog_id))
    with deriva_call():
        catalog = get_catalog(hostname, catalog_id)
        # Note: deriva-py catalog.get() is a synchronous requests call.
        schema_json = catalog.get("/schema").json()
    schema_hash = _compute_schema_hash(schema_json)
    fire_catalog_connect(hostname, catalog_id, schema_hash, schema_json)
    return schema_json


# Per-server-lifetime set of (internal_hostname, catalog_id) pairs whose
# on_catalog_connect hooks have already been fired. After first access this is
# just a set lookup -- effectively a noop.
_connected_catalogs: set[tuple[str, str]] = set()
_connect_tasks: set[asyncio.Task] = set()


def _on_catalog_access(hostname: str, catalog_id: str) -> None:
    """Callback registered with context.py; fired by get_catalog() on every call.

    Schedules a background schema fetch + on_catalog_connect hook dispatch the
    first time a given catalog is accessed. Subsequent calls are a set lookup.
    """
    key = (hostname, catalog_id)
    if key in _connected_catalogs:
        return
    _connected_catalogs.add(key)

    async def _do() -> None:
        try:
            _fetch_schema(hostname, catalog_id)
        except Exception:
            logger.debug(
                "Background on_catalog_connect failed for %s/%s",
                hostname, catalog_id, exc_info=True,
            )
            _connected_catalogs.discard(key)

    try:
        task = asyncio.get_running_loop().create_task(_do())
        _connect_tasks.add(task)
        task.add_done_callback(_connect_tasks.discard)
    except RuntimeError:
        pass  # no running event loop (e.g., during import-time tests)


def register(ctx: PluginContext) -> None:
    """Register schema introspection tools with the MCP server."""
    _set_catalog_access_fn(_on_catalog_access)

    @ctx.tool(mutates=False)
    async def get_catalog_info(hostname: str, catalog_id: str) -> str:
        """Get metadata for a DERIVA catalog: schema names and table counts.

        Fetches the full catalog schema and fires the on_catalog_connect lifecycle
        hook (used by the RAG subsystem to index the schema).

        Args:
            hostname: Hostname of the DERIVA server (e.g. "deriva.example.org").
            catalog_id: Catalog ID or alias (e.g. "1" or "my-catalog").
        """
        try:
            schema_json = _fetch_schema(hostname, catalog_id)
            schemas = [
                {
                    "schema": name,
                    "tables": len(s.get("tables", {})),
                    "comment": s.get("comment"),
                }
                for name, s in schema_json.get("schemas", {}).items()
                if name not in _SYSTEM_SCHEMAS
            ]
            return json.dumps(
                {
                    "hostname": hostname,
                    "catalog_id": catalog_id,
                    "schemas": schemas,
                }
            )
        except Exception as exc:
            logger.error("get_catalog_info failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def list_schemas(hostname: str, catalog_id: str) -> str:
        """List schema names in a DERIVA catalog.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
        """
        try:
            schema_json = _fetch_schema(hostname, catalog_id)
            names = [n for n in schema_json.get("schemas", {}) if n not in _SYSTEM_SCHEMAS]
            return json.dumps({"schemas": names})
        except Exception as exc:
            logger.error("list_schemas failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def get_schema(hostname: str, catalog_id: str, schema: str) -> str:
        """Get tables and columns for a schema.

        Returns every table in the schema with its comment and all column
        names, types, and nullability.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name (e.g. "public" or "isa").
        """
        try:
            schema_json = _fetch_schema(hostname, catalog_id)
            schema_doc = schema_json.get("schemas", {}).get(schema)
            if schema_doc is None:
                return json.dumps({"error": f"Schema not found: {schema!r}"})

            tables = [
                {
                    "table": tname,
                    "comment": tdef.get("comment"),
                    "columns": [_col_summary(c) for c in tdef.get("column_definitions", [])],
                }
                for tname, tdef in schema_doc.get("tables", {}).items()
            ]
            return json.dumps(
                {
                    "schema": schema,
                    "comment": schema_doc.get("comment"),
                    "tables": tables,
                }
            )
        except Exception as exc:
            logger.error("get_schema failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def get_table(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
    ) -> str:
        """Get the full definition of a table: columns, keys, and foreign keys.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
        """
        try:
            schema_json = _fetch_schema(hostname, catalog_id)
            schema_doc = schema_json.get("schemas", {}).get(schema)
            if schema_doc is None:
                return json.dumps({"error": f"Schema not found: {schema!r}"})
            tdef = schema_doc.get("tables", {}).get(table)
            if tdef is None:
                return json.dumps({"error": f"Table not found: {schema!r}:{table!r}"})

            keys = [{"columns": k.get("unique_columns", [])} for k in tdef.get("keys", [])]
            return json.dumps(
                {
                    "schema": schema,
                    "table": table,
                    "comment": tdef.get("comment"),
                    "kind": tdef.get("kind", "table"),
                    "columns": [_col_summary(c) for c in tdef.get("column_definitions", [])],
                    "keys": keys,
                    "foreign_keys": [_fk_summary(fk) for fk in tdef.get("foreign_keys", [])],
                }
            )
        except Exception as exc:
            logger.error("get_table failed: %s", exc)
            return json.dumps({"error": str(exc)})
