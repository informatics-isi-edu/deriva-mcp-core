from __future__ import annotations

"""Schema DDL tools for DERIVA catalogs.

Provides MCP tools for creating and altering catalog schema objects (tables,
columns). These tools make immediate HTTP requests to ERMrest -- there is no
staged model.

    create_table          -- create a new table in a schema
    add_column            -- add a column to an existing table
    set_table_description -- update the comment (description) on a table
    set_column_description -- update the comment on a column
    set_column_nullok     -- change whether a column allows NULL values

All schema-mutating tools fire the on_schema_change lifecycle hook after success
so that the RAG subsystem can reindex the updated schema.
"""

import json
import logging
from typing import TYPE_CHECKING

from deriva.core.ermrest_model import Column, ForeignKey, Table, builtin_types

from . import fmt_exc
from ..context import deriva_call, get_catalog
from ..plugin.api import fire_schema_change
from ..telemetry import audit_event

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)

# Mapping from user-supplied type name strings to ermrest_model builtin type objects.
_TYPE_MAP = {
    "text": builtin_types.text,
    "int2": builtin_types.int2,
    "int4": builtin_types.int4,
    "int8": builtin_types.int8,
    "float4": builtin_types.float4,
    "float8": builtin_types.float8,
    "boolean": builtin_types.boolean,
    "date": builtin_types.date,
    "timestamp": builtin_types.timestamp,
    "timestamptz": builtin_types.timestamptz,
    "json": builtin_types.json,
    "jsonb": builtin_types.jsonb,
    "markdown": builtin_types.markdown,
}

_VALID_TYPES = sorted(_TYPE_MAP)


def register(ctx: PluginContext) -> None:
    """Register schema DDL tools with the MCP server."""

    @ctx.tool(mutates=True)
    async def create_table(
        hostname: str,
        catalog_id: str,
        schema: str,
        table_name: str,
        columns: list[dict] | None = None,
        foreign_keys: list[dict] | None = None,
        comment: str = "",
    ) -> str:
        """Create a new table in a DERIVA catalog schema.

        System columns (RID, RCT, RCB, RMT, RMB) are added automatically by ERMrest.
        The primary key on RID is also added automatically.

        Column definition dict fields:
            name     (str, required) -- column name
            type     (str)           -- one of: text, int2, int4, int8, float4, float8,
                                       boolean, date, timestamp, timestamptz, json, jsonb,
                                       markdown  (default: text)
            nullok   (bool)          -- allow NULL values (default: True)
            default  (str)           -- default value expression (default: None)
            comment  (str)           -- column description (default: "")

        Foreign key definition dict fields:
            column              (str, required) -- column name in this table
            referenced_schema   (str, required) -- schema of the referenced table
            referenced_table    (str, required) -- referenced table name
            referenced_column   (str)           -- referenced column (default: "RID")
            on_delete           (str)           -- NO ACTION, CASCADE, or SET NULL
                                                  (default: NO ACTION)

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema to create the table in.
            table_name: Name for the new table.
            columns: List of column definition dicts (see above).
            foreign_keys: List of foreign key definition dicts (see above).
            comment: Description of the table.
        """
        try:
            col_defs = []
            if columns:
                for col in columns:
                    col_type = _TYPE_MAP.get(col.get("type", "text"), builtin_types.text)
                    col_defs.append(Column.define(
                        col["name"],
                        col_type,
                        nullok=col.get("nullok", True),
                        default=col.get("default"),
                        comment=col.get("comment", ""),
                    ))

            fkey_defs = []
            if foreign_keys:
                for fk in foreign_keys:
                    fkey_defs.append(ForeignKey.define(
                        [fk["column"]],
                        fk["referenced_schema"],
                        fk["referenced_table"],
                        [fk.get("referenced_column", "RID")],
                        on_delete=fk.get("on_delete", "NO ACTION"),
                    ))

            table_def = Table.define(
                table_name,
                column_defs=col_defs,
                fkey_defs=fkey_defs,
                comment=comment,
            )

            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                new_table = model.schemas[schema].create_table(table_def)

            fire_schema_change(hostname, catalog_id)
            audit_event(
                "schema_create_table",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table_name=table_name,
                column_count=len(columns or []),
            )
            return json.dumps({
                "status": "created",
                "schema": schema,
                "table_name": new_table.name,
                "columns": [c.name for c in new_table.columns],
            })
        except Exception as exc:
            logger.error("create_table failed: %s", exc)
            audit_event(
                "schema_create_table_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table_name=table_name,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def add_column(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        column_name: str,
        column_type: str = "text",
        nullok: bool = True,
        default: str | None = None,
        comment: str | None = None,
    ) -> str:
        """Add a new column to an existing DERIVA table.

        Valid column types: text, int2, int4, int8, float4, float8, boolean,
        date, timestamp, timestamptz, json, jsonb, markdown.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            column_name: Name for the new column.
            column_type: Data type (default: text).
            nullok: Whether NULL values are allowed (default: True).
            default: Default value expression (optional).
            comment: Column description (optional).
        """
        col_type = _TYPE_MAP.get(column_type)
        if col_type is None:
            return json.dumps({
                "error": f"Unknown column type {column_type!r}. Valid types: {_VALID_TYPES}"
            })
        try:
            col_def = Column.define(
                column_name,
                col_type,
                nullok=nullok,
                default=default,
                comment=comment or "",
            )
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                new_col = model.schemas[schema].tables[table].create_column(col_def)
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "schema_add_column",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column_name=column_name,
                column_type=column_type,
            )
            return json.dumps({
                "status": "created",
                "schema": schema,
                "table": table,
                "column_name": new_col.name,
                "column_type": column_type,
            })
        except Exception as exc:
            logger.error("add_column failed: %s", exc)
            audit_event(
                "schema_add_column_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column_name=column_name,
                column_type=column_type,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def set_table_description(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        description: str,
    ) -> str:
        """Set or update the description (comment) on a table.

        The description appears in schema introspection results and is indexed
        by the RAG subsystem for semantic search.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            description: New description text.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                model.schemas[schema].tables[table].alter(comment=description)
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "schema_set_table_description",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
            )
            return json.dumps({
                "status": "updated",
                "schema": schema,
                "table": table,
                "description": description,
            })
        except Exception as exc:
            logger.error("set_table_description failed: %s", exc)
            audit_event(
                "schema_set_table_description_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def set_column_description(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        column: str,
        description: str,
    ) -> str:
        """Set or update the description (comment) on a column.

        The description appears in schema introspection and is indexed by the
        RAG subsystem for semantic search.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            column: Column name.
            description: New description text.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                model.schemas[schema].tables[table].columns[column].alter(comment=description)
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "schema_set_column_description",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column=column,
            )
            return json.dumps({
                "status": "updated",
                "schema": schema,
                "table": table,
                "column": column,
                "description": description,
            })
        except Exception as exc:
            logger.error("set_column_description failed: %s", exc)
            audit_event(
                "schema_set_column_description_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column=column,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def set_column_nullok(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        column: str,
        nullok: bool,
    ) -> str:
        """Change whether a column allows NULL values.

        Setting nullok=False will fail if the column currently contains any NULL
        values. ERMrest will return an error listing the constraint.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            column: Column name.
            nullok: True to allow NULL; False to require a value.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                model.schemas[schema].tables[table].columns[column].alter(nullok=nullok)
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "schema_set_column_nullok",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column=column,
                nullok=nullok,
            )
            return json.dumps({
                "status": "updated",
                "schema": schema,
                "table": table,
                "column": column,
                "nullok": nullok,
            })
        except Exception as exc:
            logger.error("set_column_nullok failed: %s", exc)
            audit_event(
                "schema_set_column_nullok_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column=column,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})