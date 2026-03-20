from __future__ import annotations

"""Built-in ERMREST tool modules for deriva-mcp-core.

Each module exposes a register(ctx: PluginContext) function following the same
contract as external plugins. Built-in modules are registered directly in server.py
(not via entry points) before external plugin discovery runs.

Modules:
    catalog  -- Schema introspection (catalogs, schemas, tables, columns, keys, FKs)
    entity   -- Entity CRUD (get, insert, update, delete)
    query    -- Attribute and aggregate queries
    hatrac   -- Hatrac object store basics
"""

