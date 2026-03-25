from __future__ import annotations

"""Built-in ERMREST tool modules for deriva-mcp-core.

Each module exposes a register(ctx: PluginContext) function following the same
contract as external plugins. Built-in modules are registered directly in server.py
(not via entry points) before external plugin discovery runs.

Modules:
    catalog    -- Schema introspection (catalogs, schemas, tables, columns, keys, FKs)
    entity     -- Entity CRUD (get, insert, update, delete)
    query      -- Attribute and aggregate queries
    hatrac     -- Hatrac object store basics
    vocabulary -- Controlled vocabulary term management
    annotation -- Annotation read/write (display, visible-columns, table-display, etc.)
    schema     -- Schema DDL (create table, add column, set descriptions, nullok)
"""


def fmt_exc(exc: Exception) -> str:
    """Format an exception message for tool error responses.

    Converts bare KeyError messages (e.g. KeyError('sample') -> \"'sample'\")
    into readable strings so callers see 'schema/table/column not found: sample'
    rather than a quoted name with no context.
    """
    if isinstance(exc, KeyError):
        return f"schema, table, or column not found: {exc.args[0]!r}"
    return str(exc)
