from __future__ import annotations

"""Controlled vocabulary tools for DERIVA catalogs.

Provides MCP tools for managing vocabulary terms in DERIVA vocabulary tables.
A vocabulary table is any table following the standard DERIVA vocabulary schema
with columns: Name, ID, URI, Synonyms, Description.

    list_vocabulary_terms -- list all terms in a vocabulary table
    lookup_term           -- find a term by name or synonym
    add_term              -- insert a new term (ID and URI auto-generated)
    update_term           -- update description and/or synonyms for an existing term
    delete_term           -- delete a term (ERMrest FK constraints enforced server-side)

All tools require explicit schema + table parameters. Use list_schemas / get_table
from the catalog tools to discover vocabulary table names.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from . import fmt_exc
from ..context import deriva_call, get_catalog
from ..telemetry import audit_event

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)

# Standard DERIVA vocabulary column names. Catalogs following the DERIVA vocabulary
# convention always use these names. Non-standard vocabularies may require using
# insert_entities / update_entities directly.
_NAME = "Name"
_ID = "ID"
_URI = "URI"
_SYNONYMS = "Synonyms"
_DESCRIPTION = "Description"


def register(ctx: PluginContext) -> None:
    """Register vocabulary management tools with the MCP server."""

    @ctx.tool(mutates=False)
    async def list_vocabulary_terms(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
    ) -> str:
        """List all terms in a DERIVA vocabulary table.

        Returns all terms with their Name, ID, URI, Synonyms, and Description.
        Use get_table to verify a table is a vocabulary table (look for those columns).

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name containing the vocabulary table.
            table: Vocabulary table name (e.g., "Dataset_Type", "Tissue").
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                pb = catalog.getPathBuilder()
                path = pb.schemas[schema].tables[table]
                terms = list(path.entities().fetch())
            return json.dumps({
                "schema": schema,
                "table": table,
                "count": len(terms),
                "terms": terms,
            })
        except Exception as exc:
            logger.error("list_vocabulary_terms failed: %s", exc)
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=False)
    async def lookup_term(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        name: str,
    ) -> str:
        """Find a vocabulary term by name or synonym.

        Searches first by exact Name match, then by Synonyms. Returns the full
        term record including RID, Name, ID, URI, Synonyms, and Description.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name containing the vocabulary table.
            table: Vocabulary table name.
            name: Primary name or synonym to search for.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                pb = catalog.getPathBuilder()
                path = pb.schemas[schema].tables[table]
                # Server-side exact Name match first
                results = list(path.filter(path.Name == name).entities().fetch(limit=1))
                if results:
                    return json.dumps({"schema": schema, "table": table, "term": results[0]})
                # Fall back to client-side synonym search
                all_terms = list(path.entities().fetch())
            for term in all_terms:
                synonyms = term.get(_SYNONYMS) or []
                if isinstance(synonyms, str):
                    try:
                        synonyms = json.loads(synonyms)
                    except Exception:
                        synonyms = []
                if name in synonyms:
                    return json.dumps({"schema": schema, "table": table, "term": term})
            return json.dumps({
                "error": f"Term {name!r} not found in {schema}:{table} (checked names and synonyms)"
            })
        except Exception as exc:
            logger.error("lookup_term failed: %s", exc)
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def add_term(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        name: str,
        description: str,
        synonyms: list[str] | None = None,
    ) -> str:
        """Insert a new term into a DERIVA vocabulary table.

        ID and URI are auto-generated by ERMrest using the Name value. The term
        Name must be unique within the table. Returns the inserted term record
        including the assigned RID, ID, and URI.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name containing the vocabulary table.
            table: Vocabulary table name.
            name: Primary name for the term (must be unique).
            description: Human-readable description of the term.
            synonyms: Optional list of alternative names for the term.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                pb = catalog.getPathBuilder()
                path = pb.schemas[schema].tables[table]
                inserted = path.insert(
                    [{
                        _NAME: name,
                        _DESCRIPTION: description,
                        _SYNONYMS: synonyms or [],
                    }],
                    defaults={_ID, _URI},
                )
                term = inserted[0] if inserted else {}
            audit_event(
                "vocabulary_add_term",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                term_name=name,
            )
            return json.dumps({
                "status": "created",
                "schema": schema,
                "table": table,
                "term": term,
            })
        except Exception as exc:
            logger.error("add_term failed: %s", exc)
            audit_event(
                "vocabulary_add_term_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                term_name=name,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def update_term(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        name: str,
        description: str | None = None,
        synonyms: list[str] | None = None,
    ) -> str:
        """Update the description and/or synonyms of an existing vocabulary term.

        Only the fields you provide are changed -- omitting description leaves the
        current description unchanged, and omitting synonyms leaves the current
        synonyms unchanged. To clear synonyms pass an empty list [].

        To add or remove a single synonym without replacing the full list:
            1. Call lookup_term to get the current Synonyms array.
            2. Modify the list.
            3. Call update_term with the modified list.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name containing the vocabulary table.
            table: Vocabulary table name.
            name: Primary name of the term to update.
            description: New description (omit to leave unchanged).
            synonyms: Full replacement synonyms list (omit to leave unchanged).
        """
        if description is None and synonyms is None:
            return json.dumps({"error": "at least one of description or synonyms must be provided"})
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                pb = catalog.getPathBuilder()
                path = pb.schemas[schema].tables[table]
                rows = list(path.filter(path.Name == name).entities().fetch(limit=1))
                if not rows:
                    return json.dumps({"error": f"Term {name!r} not found in {schema}:{table}"})
                rid = rows[0]["RID"]
                update: dict[str, Any] = {"RID": rid}
                if description is not None:
                    update[_DESCRIPTION] = description
                if synonyms is not None:
                    update[_SYNONYMS] = synonyms
                updated = list(path.update([update]))
                term = updated[0] if updated else {}
            audit_event(
                "vocabulary_update_term",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                term_name=name,
            )
            return json.dumps({
                "status": "updated",
                "schema": schema,
                "table": table,
                "term": term,
            })
        except Exception as exc:
            logger.error("update_term failed: %s", exc)
            audit_event(
                "vocabulary_update_term_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                term_name=name,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def delete_term(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        name: str,
    ) -> str:
        """Delete a term from a vocabulary table.

        The term is identified by its primary Name. If other records in the catalog
        reference this term via a foreign key, ERMrest will refuse the deletion
        with a constraint violation error listing which table holds the references.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name containing the vocabulary table.
            table: Vocabulary table name.
            name: Primary name of the term to delete.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                pb = catalog.getPathBuilder()
                path = pb.schemas[schema].tables[table]
                path.filter(path.Name == name).delete()
            audit_event(
                "vocabulary_delete_term",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                term_name=name,
            )
            return json.dumps({
                "status": "deleted",
                "schema": schema,
                "table": table,
                "name": name,
            })
        except Exception as exc:
            logger.error("delete_term failed: %s", exc)
            audit_event(
                "vocabulary_delete_term_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                term_name=name,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})