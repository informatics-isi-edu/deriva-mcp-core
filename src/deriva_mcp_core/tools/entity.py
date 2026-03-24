from __future__ import annotations

"""Entity CRUD tools for DERIVA catalogs.

Provides MCP tools for ERMRest entity operations:
    get_entities     -- Retrieve entities from a table (with optional filters)
    insert_entities  -- Insert new entity records (POST)
    update_entities  -- Update existing entity records (PUT)
    delete_entities  -- Delete entity records matching filters (DELETE)
"""

import json
import logging
import urllib.parse
from typing import TYPE_CHECKING, Any

from ..context import get_deriva_server
from ..telemetry import audit_event

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)


def _enc(value: Any) -> str:
    """Percent-encode a value for an ERMREST URL path segment."""
    return urllib.parse.quote(str(value), safe="")


def _entity_url(schema: str, table: str, filters: dict[str, Any] | None) -> str:
    """Build an ERMREST /entity URL with optional equality filter predicates."""
    base = f"/entity/{_enc(schema)}:{_enc(table)}"
    if filters:
        predicates = "&".join(f"{_enc(c)}={_enc(v)}" for c, v in filters.items())
        base += f"/{predicates}"
    return base


def register(ctx: PluginContext) -> None:
    """Register entity CRUD tools with the MCP server."""

    @ctx.tool(mutates=False)
    async def get_entities(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> str:
        """Retrieve entities from a DERIVA table.

        Returns up to `limit` rows from the given table. Use `filters` for
        simple column-equality filtering. For complex queries (path traversal,
        aggregates, projections), use query_attribute or query_aggregate.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name (e.g. "public" or "isa").
            table: Table name.
            filters: Optional column equality filters {column: value}.
            limit: Maximum entities to return (default 100, max 1000).
        """
        try:
            catalog = get_deriva_server(hostname).connect_ermrest(catalog_id)
            limit = min(limit, 1000)
            url = _entity_url(schema, table, filters) + f"?limit={limit}"
            entities = catalog.get(url).json()
            return json.dumps(
                {
                    "schema": schema,
                    "table": table,
                    "count": len(entities),
                    "entities": entities,
                }
            )
        except Exception as exc:
            logger.error("get_entities failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=True)
    async def insert_entities(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        entities: list[dict[str, Any]],
    ) -> str:
        """Insert new entity records into a DERIVA table.

        Sends a POST request with the given entity list. ERMREST assigns RID
        and system columns (RCT, RCB, RMT, RMB) automatically.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            entities: List of entity dicts. Omit RID and system columns.
        """
        try:
            catalog = get_deriva_server(hostname).connect_ermrest(catalog_id)
            url = f"/entity/{_enc(schema)}:{_enc(table)}"
            inserted = catalog.post(url, json=entities).json()
            audit_event(
                "entity_insert",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                input_row_count=len(entities),
                inserted_count=len(inserted),
            )
            return json.dumps(
                {
                    "status": "inserted",
                    "schema": schema,
                    "table": table,
                    "inserted_count": len(inserted),
                    "rids": [r.get("RID") for r in inserted],
                }
            )
        except Exception as exc:
            logger.error("insert_entities failed: %s", exc)
            audit_event(
                "entity_insert_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                input_row_count=len(entities),
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=True)
    async def update_entities(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        entities: list[dict[str, Any]],
    ) -> str:
        """Update existing entity records in a DERIVA table.

        Sends a PUT request (upsert semantics): entities with a matching RID
        are updated; entities without a RID that match on unique key are also
        updated. Each entity dict must include enough identifying columns for
        ERMREST to locate the record (typically RID).

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            entities: List of entity dicts including RID.
        """
        try:
            catalog = get_deriva_server(hostname).connect_ermrest(catalog_id)
            url = f"/entity/{_enc(schema)}:{_enc(table)}"
            updated = catalog.put(url, json=entities).json()
            audit_event(
                "entity_update",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                input_row_count=len(entities),
                updated_count=len(updated),
            )
            return json.dumps(
                {
                    "status": "updated",
                    "schema": schema,
                    "table": table,
                    "updated_count": len(updated),
                    "rids": [r.get("RID") for r in updated],
                }
            )
        except Exception as exc:
            logger.error("update_entities failed: %s", exc)
            audit_event(
                "entity_update_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                input_row_count=len(entities),
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=True)
    async def delete_entities(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        filters: dict[str, Any],
    ) -> str:
        """Delete entity records matching the given filters.

        Filters are required to prevent accidental full-table deletion.
        For fine-grained control, use RID as the filter key.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            filters: Column equality filters {column: value}. Must be non-empty.
        """
        try:
            if not filters:
                return json.dumps(
                    {"error": "filters must be non-empty to prevent full-table deletion"}
                )
            catalog = get_deriva_server(hostname).connect_ermrest(catalog_id)
            url = _entity_url(schema, table, filters)
            catalog.delete(url)
            audit_event(
                "entity_delete",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                filters=filters,
            )
            return json.dumps(
                {
                    "status": "deleted",
                    "schema": schema,
                    "table": table,
                    "filters": filters,
                }
            )
        except Exception as exc:
            logger.error("delete_entities failed: %s", exc)
            audit_event(
                "entity_delete_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                filters=filters,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": str(exc)})
