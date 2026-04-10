from __future__ import annotations

"""Entity CRUD tools for DERIVA catalogs.

Provides MCP tools for ERMrest entity operations via the deriva-py datapath API:
    get_entities     -- Retrieve entities from a table (with optional filters)
    insert_entities  -- Insert new entity records
    update_entities  -- Update existing entity records (sparse -- only supplied columns change)
    delete_entities  -- Delete entity records matching filters

All tools use catalog.getPathBuilder() rather than raw ERMrest URLs. In particular,
update_entities uses EntitySet.update() which sends PUT /attributegroup with only the
supplied columns as targets, so unspecified nullable columns are never nulled out.

When a schema or table name cannot be found, the error response includes RAG-based
suggestions (similar names from the indexed schema) when the RAG subsystem is enabled.
"""

import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from . import fmt_exc
from ..context import deriva_call, get_catalog
from ..rag import get_rag_store
from ..telemetry import audit_event

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)

# ERMrest error patterns that indicate a schema or table was not found.
_NOT_FOUND_PATTERNS = [
    re.compile(r"table.+not found", re.IGNORECASE),
    re.compile(r"schema.+not found", re.IGNORECASE),
    re.compile(r"no such table", re.IGNORECASE),
    re.compile(r"does not exist", re.IGNORECASE),
    re.compile(r"not found in schema", re.IGNORECASE),
    re.compile(r"404", re.IGNORECASE),
]


def _is_not_found_error(exc: Exception) -> bool:
    """Return True if exc looks like a schema/table not-found error from ERMrest.

    Catches both HTTP-level not-found responses (404, "does not exist", etc.) and
    KeyError raised by the deriva-py PathBuilder when a schema or table name is
    absent from the catalog model (e.g. KeyError: 'sample').
    """
    if isinstance(exc, KeyError):
        return True
    msg = str(exc)
    return any(p.search(msg) for p in _NOT_FOUND_PATTERNS)


async def _rag_suggestions(
    query: str,
    hostname: str,
    catalog_id: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return schema entity suggestions from the RAG store for query.

    Scoped to schema chunks for the given catalog by source prefix so that
    documentation chunks cannot crowd out catalog-specific results.
    Returns an empty list when the RAG subsystem is disabled or the store
    has no schema chunks indexed yet for this catalog.
    """
    store = get_rag_store()
    if store is None:
        logger.debug("_rag_suggestions: RAG store not initialized, skipping suggestions")
        return []
    # Source names are "schema:{hostname}:{catalog_id}:{hash}" -- filter by prefix
    # so we search only this catalog's schema chunks, not docs or other catalogs.
    source_prefix = f"schema:{hostname}:{catalog_id}:"
    try:
        # Pre-filter to schema doc_type so doc chunks cannot crowd out catalog schema
        # chunks. Python-side source prefix narrows further to this specific catalog.
        candidates = await store.search(query, limit=100, where={"doc_type": "schema"})
        results = [r for r in candidates if r.source.startswith(source_prefix)][:limit]
        logger.debug(
            "_rag_suggestions: %d candidate(s), %d scoped to %s",
            len(candidates), len(results), source_prefix,
        )
        for i, r in enumerate(results):
            logger.debug(
                "_rag_suggestions: result[%d] score=%.4f heading=%r",
                i, r.score, r.metadata.get("section_heading", ""),
            )
        suggestions = []
        for r in results:
            heading = r.metadata.get("section_heading") or r.text[:60]
            name = heading.split("(")[0].strip()
            for prefix in ("Table: ", "Schema: "):
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
            suggestions.append({
                "name": name,
                "description": r.text[:200],
                "relevance": r.score,
            })
        if not suggestions:
            logger.warning(
                "_rag_suggestions: no schema chunks found for %s query=%r",
                source_prefix, query,
            )
        return suggestions
    except Exception:
        logger.warning("_rag_suggestions: search raised unexpected exception", exc_info=True)
        return []


async def _entity_error(
    exc: Exception,
    hostname: str,
    catalog_id: str,
    schema: str,
    table: str,
    operation: str,
) -> dict[str, Any]:
    """Build an error result dict, appending RAG suggestions for not-found errors."""
    logger.error("%s failed: %s", operation, exc)
    result: dict[str, Any] = {"error": fmt_exc(exc)}
    if _is_not_found_error(exc):
        suggestions = await _rag_suggestions(
            f"Schema: {schema} Table: {schema}:{table}", hostname, catalog_id
        )
        if suggestions:
            # Prefer suggestions whose name matches the requested schema so that
            # embedding rank does not override an obvious keyword match.
            schema_lower = schema.lower()
            def _rank(s: dict) -> int:
                name = s["name"].lower()
                if name.startswith(schema_lower + ":"):
                    return 0
                if name == schema_lower:
                    return 1
                return 2
            suggestions.sort(key=_rank)
            top = suggestions[0]["name"]
            # If the top suggestion is a Schema:Table pair, surface the schema
            # name explicitly so the caller can correct the schema directly.
            if ":" in top:
                schema_part = top.split(":")[0]
                hint = f"Did you mean schema: '{schema_part}'? (e.g. {top})"
            else:
                hint = f"Did you mean: '{top}'?"
            result["hint"] = hint
            result["suggestions"] = suggestions
    return result


def _build_filter_segment(filters: dict[str, Any]) -> str:
    """Build an ERMrest filter path segment from a column-value dict.

    Handles scalar values and list values (using ERMrest any() syntax).
    All values are URL-encoded for safe inclusion in the path.
    """
    parts: list[str] = []
    for col, val in filters.items():
        if isinstance(val, list):
            encoded = ",".join(quote(str(v), safe="") for v in val)
            parts.append(f"/{col}=any({encoded})")
        else:
            parts.append(f"/{col}={quote(str(val), safe='')}")
    return "".join(parts)


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
        after_rid: str | None = None,
        preflight_count: bool = False,
    ) -> str:
        """Retrieve entities from a DERIVA table.

        See ENTITY TOOL GUIDE for preflight rules, pagination, and display
        rules.

        Returns up to `limit` rows sorted by RID with optional equality filters
        and cursor-based pagination via `after_rid`.

        PREFLIGHT COUNT RULE: When the table's row count is unknown, you MUST
        call with preflight_count=True first. It returns only the count (never
        fetches rows). Present the count, confirm the limit, then call again
        with preflight_count=False.

        Args:
            hostname: DERIVA server hostname.
            catalog_id: Catalog ID, alias, or ID@snaptime (Crockford base32 --
                call resolve_snaptime to convert a date).
            schema: Schema name.
            table: Table name.
            filters: Optional equality filters {column: value}.
            limit: Max entities to return (default 100, max 1000).
            after_rid: RID of last row from previous page to advance cursor.
            preflight_count: If True, return count only. Default False.
        """
        try:
            effective_limit = min(limit, 1000)
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)

                filter_seg = ""
                if filters:
                    filter_seg = _build_filter_segment(filters)

                if preflight_count:
                    # Count only -- never fetch entities on a preflight call.
                    count_result = catalog.get(
                        f"/aggregate/{schema}:{table}{filter_seg}/cnt:=cnt(RID)"
                    ).json()
                    total_count: int = count_result[0]["cnt"] if count_result else 0
                    return json.dumps({
                        "schema": schema,
                        "table": table,
                        "total_count": total_count,
                        "entities_fetched": False,
                        "action_required": (
                            f"Found {total_count} rows. Present this count to the user "
                            f"and ask what limit to use before calling get_entities "
                            f"again with preflight_count=False. Use after_rid (the last "
                            f"RID of each page) to paginate through large result sets."
                        ),
                    })

                after_seg = f"@after({after_rid})" if after_rid is not None else ""
                url = (
                    f"/entity/{schema}:{table}{filter_seg}"
                    f"@sort(RID){after_seg}?limit={effective_limit}"
                )
                entities = catalog.get(url).json()

            returned_count = len(entities)
            truncated = returned_count == effective_limit
            result: dict = {
                "schema": schema,
                "table": table,
                "returned_count": returned_count,
                "truncated": truncated,
                "entities": entities,
            }
            if truncated and entities:
                result["next_after_rid"] = entities[-1]["RID"]
            return json.dumps(result)
        except Exception as exc:
            return json.dumps(await _entity_error(exc, hostname, catalog_id, schema, table, "get_entities"))

    @ctx.tool(mutates=True)
    async def insert_entities(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        entities: list[dict[str, Any]],
    ) -> str:
        """Insert new entity records into a DERIVA table.

        See ENTITY TOOL GUIDE for entity operation patterns.

        Omit RID and system columns (RCT, RCB, RMT, RMB) -- ERMrest
        generates them automatically.

        Args:
            hostname: DERIVA server hostname.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            entities: List of entity dicts. Omit RID and system columns.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                pb = catalog.getPathBuilder()
                path = pb.schemas[schema].tables[table]
                inserted = list(path.insert(entities))
            audit_event(
                "entity_insert",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                input_row_count=len(entities),
                inserted_count=len(inserted),
            )
            return json.dumps({
                "status": "inserted",
                "schema": schema,
                "table": table,
                "inserted_count": len(inserted),
                "rids": [r.get("RID") for r in inserted],
            })
        except Exception as exc:
            audit_event(
                "entity_insert_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                input_row_count=len(entities),
                error_type=type(exc).__name__,
            )
            return json.dumps(await _entity_error(exc, hostname, catalog_id, schema, table, "insert_entities"))

    @ctx.tool(mutates=True)
    async def update_entities(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        entities: list[dict[str, Any]],
    ) -> str:
        """Update existing entity records in a DERIVA table.

        See ENTITY TOOL GUIDE for entity operation patterns.

        Uses sparse updates (PUT /attributegroup) -- only columns present in
        each dict are modified. Each entity dict must include RID.

        Args:
            hostname: DERIVA server hostname.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            entities: List of entity dicts with RID plus columns to update.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                pb = catalog.getPathBuilder()
                path = pb.schemas[schema].tables[table]
                updated = list(path.update(entities))
            audit_event(
                "entity_update",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                input_row_count=len(entities),
                updated_count=len(updated),
            )
            return json.dumps({
                "status": "updated",
                "schema": schema,
                "table": table,
                "updated_count": len(updated),
                "rids": [r.get("RID") for r in updated],
            })
        except Exception as exc:
            audit_event(
                "entity_update_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                input_row_count=len(entities),
                error_type=type(exc).__name__,
            )
            return json.dumps(await _entity_error(exc, hostname, catalog_id, schema, table, "update_entities"))

    @ctx.tool(mutates=True)
    async def delete_entities(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        filters: dict[str, Any],
    ) -> str:
        """Delete entity records matching the given filters.

        See ENTITY TOOL GUIDE for entity operation patterns.

        Filters are required (non-empty) to prevent accidental full-table
        deletion. Use filters={"RID": rid} for single-row deletes.

        Args:
            hostname: DERIVA server hostname.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            filters: Column equality filters {column: value}. Must be non-empty.
        """
        if not filters:
            return json.dumps({"error": "filters must be non-empty to prevent full-table deletion"})
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                pb = catalog.getPathBuilder()
                path = pb.schemas[schema].tables[table]
                for col, val in filters.items():
                    path = path.filter(getattr(path, col) == val)
                path.delete()
            audit_event(
                "entity_delete",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                filters=filters,
            )
            return json.dumps({
                "status": "deleted",
                "schema": schema,
                "table": table,
                "filters": filters,
            })
        except Exception as exc:
            audit_event(
                "entity_delete_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                filters=filters,
                error_type=type(exc).__name__,
            )
            return json.dumps(await _entity_error(exc, hostname, catalog_id, schema, table, "delete_entities"))