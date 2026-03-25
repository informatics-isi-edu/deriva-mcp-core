from __future__ import annotations

"""Annotation management tools for DERIVA catalogs.

Provides MCP tools for reading and writing ERMrest annotation tags that control
how Chaise renders catalog objects (display names, visible columns, sort order, etc.).

Read tools (mutates=False):
    get_table_annotations             -- all display annotations on a table
    get_column_annotations            -- all display annotations on a column
    list_foreign_keys                 -- outbound and inbound FKs (for constraint names)
    get_handlebars_template_variables -- available template variables for a table

Write tools (mutates=True) -- each call applies immediately, no staging:
    set_display_annotation      -- full display annotation on a table or column
    set_table_display_name      -- convenience: sets {"name": ...} in display annotation
    set_row_name_pattern        -- convenience: sets row_name in table-display annotation
    set_column_display_name     -- convenience: sets {"name": ...} in column display annotation
    set_visible_columns         -- full visible-columns annotation replacement
    add_visible_column          -- splice one column into visible-columns for a context
    remove_visible_column       -- remove one column from visible-columns for a context
    set_visible_foreign_keys    -- full visible-foreign-keys annotation replacement
    add_visible_foreign_key     -- splice one FK into visible-foreign-keys for a context
    remove_visible_foreign_key  -- remove one FK from visible-foreign-keys for a context
    set_table_display           -- full table-display annotation replacement
    set_column_display          -- full column-display annotation replacement

Write tools differ from the deriva-mcp prototype in one key way: changes are applied
immediately to ERMrest on each call. There is no staged model and no separate
apply_annotations() step -- each write tool performs getCatalogModel() + modify + apply().
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from . import fmt_exc
from ..context import deriva_call, get_catalog
from ..plugin.api import fire_schema_change
from ..telemetry import audit_event

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)

_DISPLAY = "tag:isrd.isi.edu,2015:display"
_VISIBLE_COLUMNS = "tag:isrd.isi.edu,2016:visible-columns"
_VISIBLE_FOREIGN_KEYS = "tag:isrd.isi.edu,2016:visible-foreign-keys"
_TABLE_DISPLAY = "tag:isrd.isi.edu,2016:table-display"
_COLUMN_DISPLAY = "tag:isrd.isi.edu,2016:column-display"


def register(ctx: PluginContext) -> None:
    """Register annotation management tools with the MCP server."""

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------

    @ctx.tool(mutates=False)
    async def get_table_annotations(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
    ) -> str:
        """Get all display-related annotations for a table.

        Returns the current display, visible-columns, visible-foreign-keys, and
        table-display annotations. Missing annotations are returned as null.
        Use this before calling set_visible_columns or set_visible_foreign_keys
        to read the current value when you want to splice a single entry.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
            return json.dumps({
                "schema": schema,
                "table": table,
                "display": tbl.annotations.get(_DISPLAY),
                "visible_columns": tbl.annotations.get(_VISIBLE_COLUMNS),
                "visible_foreign_keys": tbl.annotations.get(_VISIBLE_FOREIGN_KEYS),
                "table_display": tbl.annotations.get(_TABLE_DISPLAY),
            })
        except Exception as exc:
            logger.error("get_table_annotations failed: %s", exc)
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=False)
    async def get_column_annotations(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        column: str,
    ) -> str:
        """Get all display-related annotations for a column.

        Returns the current display and column-display annotations.
        Missing annotations are returned as null.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            column: Column name.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                col = model.schemas[schema].tables[table].columns[column]
            return json.dumps({
                "schema": schema,
                "table": table,
                "column": column,
                "display": col.annotations.get(_DISPLAY),
                "column_display": col.annotations.get(_COLUMN_DISPLAY),
            })
        except Exception as exc:
            logger.error("get_column_annotations failed: %s", exc)
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=False)
    async def list_foreign_keys(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
    ) -> str:
        """List outbound and inbound foreign keys for a table.

        Returns constraint names, column mappings, and referenced tables for
        both outbound (this table references another) and inbound (another table
        references this one) foreign keys.

        Constraint names are required when writing visible-columns or
        visible-foreign-keys annotations that reference foreign keys as
        ["schema_name", "constraint_name"] pairs.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]

            outbound = []
            for fk in tbl.foreign_keys:
                outbound.append({
                    "constraint_name": [fk.constraint_schema.name, fk.constraint_name],
                    "from_columns": [c.name for c in fk.columns],
                    "to_schema": fk.pk_table.schema.name,
                    "to_table": fk.pk_table.name,
                    "to_columns": [c.name for c in fk.referenced_columns],
                })

            inbound = []
            for fk in tbl.referenced_by:
                inbound.append({
                    "constraint_name": [fk.constraint_schema.name, fk.constraint_name],
                    "from_schema": fk.table.schema.name,
                    "from_table": fk.table.name,
                    "from_columns": [c.name for c in fk.columns],
                    "to_columns": [c.name for c in fk.referenced_columns],
                })

            return json.dumps({
                "schema": schema,
                "table": table,
                "outbound": outbound,
                "inbound": inbound,
            })
        except Exception as exc:
            logger.error("list_foreign_keys failed: %s", exc)
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=False)
    async def get_handlebars_template_variables(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
    ) -> str:
        """Return all Handlebars template variables available for a table.

        Useful when writing row_markdown_pattern, markdown_pattern, or other
        Handlebars templates for display annotations. Returns column names
        (with template syntax), outbound foreign key paths, and special
        built-in variables.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]

            columns = []
            for col in tbl.columns:
                columns.append({
                    "name": col.name,
                    "type": col.type.typename,
                    "template": "{{{" + col.name + "}}}",
                    "row_template": "{{{_row." + col.name + "}}}",
                })

            fkeys = []
            for fk in tbl.foreign_keys:
                sname = fk.constraint_schema.name
                cname = fk.constraint_name
                fk_path = f"$fkeys.{sname}.{cname}"
                ref_cols = [c.name for c in fk.pk_table.columns]
                fkeys.append({
                    "constraint": [sname, cname],
                    "from_columns": [c.name for c in fk.columns],
                    "to_table": fk.pk_table.name,
                    "row_name_template": "{{{" + fk_path + ".rowName}}}",
                    "example_value_templates": [
                        "{{{" + fk_path + ".values." + c + "}}}" for c in ref_cols[:3]
                    ],
                })

            return json.dumps({
                "schema": schema,
                "table": table,
                "columns": columns,
                "foreign_keys": fkeys,
                "special_variables": {
                    "_value": "{{{_value}}} -- current column value (in column_display)",
                    "_row": "{{{_row.column_name}}} -- another column in the same row",
                    "$catalog.id": "{{{$catalog.id}}} -- catalog ID",
                },
                "helper_examples": {
                    "conditional": "{{#if column}}...{{else}}...{{/if}}",
                    "iteration": "{{#each array}}{{{this}}}{{/each}}",
                    "date_format": "{{formatDate RCT 'YYYY-MM-DD'}}",
                },
            })
        except Exception as exc:
            logger.error("get_handlebars_template_variables failed: %s", exc)
            return json.dumps({"error": fmt_exc(exc)})

    # ------------------------------------------------------------------
    # Write tools -- full annotation replacement
    # ------------------------------------------------------------------

    @ctx.tool(mutates=True)
    async def set_display_annotation(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        annotation: dict[str, Any] | None,
        column: str | None = None,
    ) -> str:
        """Set the display annotation (tag:isrd.isi.edu,2015:display) on a table or column.

        Pass annotation=null to remove the annotation entirely.

        For common single-property operations, prefer the convenience tools:
            set_table_display_name  -- sets {"name": "..."} on the table
            set_column_display_name -- sets {"name": "..."} on a column

        Display annotation schema:
            {
              "name": "string",            // display name (mutually exclusive with markdown_name)
              "markdown_name": "string",   // markdown display name
              "name_style": {
                "underline_space": true,   // replace underscores with spaces
                "title_case": true
              },
              "comment": "string",         // tooltip text
              "show_null": {"*": true}     // per-context null display behavior
            }

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            annotation: Display annotation value, or null to remove.
            column: Column name. If provided, sets annotation on the column;
                otherwise sets it on the table.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
                if column:
                    target_obj = tbl.columns[column]
                    target_label = f"{table}.{column}"
                else:
                    target_obj = tbl
                    target_label = table
                if annotation is None:
                    target_obj.annotations.pop(_DISPLAY, None)
                else:
                    target_obj.annotations[_DISPLAY] = annotation
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_set_display",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column=column,
            )
            return json.dumps({"status": "applied", "target": target_label, "annotation": "display"})
        except Exception as exc:
            logger.error("set_display_annotation failed: %s", exc)
            audit_event(
                "annotation_set_display_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column=column,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def set_table_display_name(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        display_name: str,
    ) -> str:
        """Set the display name shown in the Chaise UI for a table.

        Convenience wrapper for set_display_annotation that sets only the "name"
        property. Preserves all other existing display annotation properties.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            display_name: Human-readable name to show in the UI.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
                current = dict(tbl.annotations.get(_DISPLAY) or {})
                current["name"] = display_name
                tbl.annotations[_DISPLAY] = current
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_set_table_display_name",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
            )
            return json.dumps({
                "status": "applied",
                "table": table,
                "display_name": display_name,
            })
        except Exception as exc:
            logger.error("set_table_display_name failed: %s", exc)
            audit_event(
                "annotation_set_table_display_name_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def set_row_name_pattern(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        pattern: str,
    ) -> str:
        """Set the Handlebars pattern used to display row names in the Chaise UI.

        Convenience wrapper for set_table_display that sets only the row_name
        section. Preserves all other existing table-display annotation properties.

        Use get_handlebars_template_variables to see available column and FK
        variable names for the template.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            pattern: Handlebars template string, e.g. "{{{Name}}}" or
                "{{{FirstName}}} {{{LastName}}} ({{{RID}}})".
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
                current = dict(tbl.annotations.get(_TABLE_DISPLAY) or {})
                current["row_name"] = {"row_markdown_pattern": pattern}
                tbl.annotations[_TABLE_DISPLAY] = current
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_set_row_name_pattern",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
            )
            return json.dumps({"status": "applied", "table": table, "pattern": pattern})
        except Exception as exc:
            logger.error("set_row_name_pattern failed: %s", exc)
            audit_event(
                "annotation_set_row_name_pattern_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def set_column_display_name(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        column: str,
        display_name: str,
    ) -> str:
        """Set the display name shown in the Chaise UI for a column.

        Convenience wrapper for set_display_annotation that sets only the "name"
        property on the column. Preserves all other existing display annotation
        properties for the column.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            column: Column name.
            display_name: Human-readable column header to show in the UI.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                col = model.schemas[schema].tables[table].columns[column]
                current = dict(col.annotations.get(_DISPLAY) or {})
                current["name"] = display_name
                col.annotations[_DISPLAY] = current
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_set_column_display_name",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column=column,
            )
            return json.dumps({
                "status": "applied",
                "table": table,
                "column": column,
                "display_name": display_name,
            })
        except Exception as exc:
            logger.error("set_column_display_name failed: %s", exc)
            audit_event(
                "annotation_set_column_display_name_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column=column,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def set_visible_columns(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        annotation: dict[str, Any] | None,
    ) -> str:
        """Set the full visible-columns annotation (tag:isrd.isi.edu,2016:visible-columns).

        Replaces the entire annotation. Pass annotation=null to remove it entirely.

        To add or remove a single column without replacing the full annotation:
            Use add_visible_column or remove_visible_column instead.

        Visible-columns schema:
            {
              "compact": [...],    // columns for list/compact view
              "detailed": [...],   // columns for record/detailed view
              "entry": [...],      // columns for create/edit forms
              "filter": {"and": [...]}  // faceted search configuration
            }

        Column directive formats in context lists:
            "column_name"                           -- simple column
            ["schema_name", "fkey_constraint_name"] -- outbound FK as entity
            {"source": "column_name", ...}          -- pseudo-column with options

        Use list_foreign_keys to get constraint names for FK directives.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            annotation: Full visible-columns annotation value, or null to remove.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
                if annotation is None:
                    tbl.annotations.pop(_VISIBLE_COLUMNS, None)
                else:
                    tbl.annotations[_VISIBLE_COLUMNS] = annotation
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_set_visible_columns",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
            )
            return json.dumps({"status": "applied", "table": table, "annotation": "visible-columns"})
        except Exception as exc:
            logger.error("set_visible_columns failed: %s", exc)
            audit_event(
                "annotation_set_visible_columns_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def add_visible_column(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        context: str,
        column: str | list | dict,
        position: int | None = None,
    ) -> str:
        """Add a single entry to the visible-columns list for one context.

        Reads the current visible-columns annotation, splices in the new column
        at the specified position (or appends it), and writes the result back.
        The other context lists are preserved unchanged.

        Column directive formats:
            "column_name"                           -- simple column name (string)
            ["schema_name", "fkey_constraint_name"] -- outbound FK as entity
            {"source": "column_name", ...}          -- pseudo-column with options

        Use list_foreign_keys to get constraint names for FK directives.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            context: Annotation context to modify, e.g. "compact", "detailed", "entry".
            column: Column directive to add (string, list, or dict -- see above).
            position: Zero-based insert position; omit to append.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
                visible = dict(tbl.annotations.get(_VISIBLE_COLUMNS) or {})
                ctx_list = list(visible.get(context) or [])
                if position is not None:
                    ctx_list.insert(position, column)
                else:
                    ctx_list.append(column)
                visible[context] = ctx_list
                tbl.annotations[_VISIBLE_COLUMNS] = visible
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_add_visible_column",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                context=context,
            )
            return json.dumps({
                "status": "applied",
                "table": table,
                "context": context,
                "updated_list": ctx_list,
            })
        except Exception as exc:
            logger.error("add_visible_column failed: %s", exc)
            audit_event(
                "annotation_add_visible_column_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                context=context,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def remove_visible_column(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        context: str,
        column: str | list | int,
    ) -> str:
        """Remove a single entry from the visible-columns list for one context.

        Reads the current visible-columns annotation, removes the matching entry,
        and writes the result back. Other context lists are preserved unchanged.

        Identify the column to remove by:
            "column_name"  -- removes the first matching string entry
            ["s", "fkey"]  -- removes the first matching list entry
            integer        -- removes by zero-based index position

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            context: Annotation context to modify, e.g. "compact", "detailed".
            column: Column to remove: string name, list [schema, constraint], or index.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
                visible = tbl.annotations.get(_VISIBLE_COLUMNS)
                if not visible:
                    return json.dumps({"error": f"Table {table!r} has no visible-columns annotation"})
                ctx_list = list(visible.get(context) or [])
                if isinstance(column, int):
                    if 0 <= column < len(ctx_list):
                        ctx_list.pop(column)
                    else:
                        return json.dumps({"error": f"Index {column} out of range (list has {len(ctx_list)} items)"})
                else:
                    for i, item in enumerate(ctx_list):
                        if item == column:
                            ctx_list.pop(i)
                            break
                    else:
                        return json.dumps({"error": f"Column {column!r} not found in context {context!r}"})
                visible = dict(visible)
                visible[context] = ctx_list
                tbl.annotations[_VISIBLE_COLUMNS] = visible
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_remove_visible_column",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                context=context,
            )
            return json.dumps({
                "status": "applied",
                "table": table,
                "context": context,
                "updated_list": ctx_list,
            })
        except Exception as exc:
            logger.error("remove_visible_column failed: %s", exc)
            audit_event(
                "annotation_remove_visible_column_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                context=context,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def set_visible_foreign_keys(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        annotation: dict[str, Any] | None,
    ) -> str:
        """Set the full visible-foreign-keys annotation (tag:isrd.isi.edu,2016:visible-foreign-keys).

        Replaces the entire annotation. Pass annotation=null to remove it entirely.

        To add or remove a single related table without replacing the full annotation:
            Use add_visible_foreign_key or remove_visible_foreign_key instead.

        Visible-foreign-keys schema:
            {
              "detailed": [...],   // related tables shown in detailed/record view
              "*": [...]           // default for all contexts
            }

        Foreign key directive formats in context lists:
            ["schema_name", "fkey_constraint_name"]  -- inbound FK reference
            {"source": [...], "markdown_name": "..."}  -- pseudo-column for complex paths

        Use list_foreign_keys to get constraint names (look in the "inbound" list
        for this table's inbound foreign keys).

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            annotation: Full visible-foreign-keys annotation value, or null to remove.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
                if annotation is None:
                    tbl.annotations.pop(_VISIBLE_FOREIGN_KEYS, None)
                else:
                    tbl.annotations[_VISIBLE_FOREIGN_KEYS] = annotation
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_set_visible_foreign_keys",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
            )
            return json.dumps({"status": "applied", "table": table, "annotation": "visible-foreign-keys"})
        except Exception as exc:
            logger.error("set_visible_foreign_keys failed: %s", exc)
            audit_event(
                "annotation_set_visible_foreign_keys_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def add_visible_foreign_key(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        context: str,
        foreign_key: list | dict,
        position: int | None = None,
    ) -> str:
        """Add a single related-table entry to the visible-foreign-keys list for one context.

        Reads the current visible-foreign-keys annotation, splices in the new entry,
        and writes the result back. Other context lists are preserved unchanged.

        Foreign key directive formats:
            ["schema_name", "fkey_constraint_name"]  -- inbound FK reference
            {"source": [...], ...}                   -- pseudo-column for complex paths

        Use list_foreign_keys to find inbound FK constraint names for this table.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            context: Annotation context to modify, e.g. "detailed", "*".
            foreign_key: FK directive to add (list or dict -- see above).
            position: Zero-based insert position; omit to append.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
                visible = dict(tbl.annotations.get(_VISIBLE_FOREIGN_KEYS) or {})
                ctx_list = list(visible.get(context) or [])
                if position is not None:
                    ctx_list.insert(position, foreign_key)
                else:
                    ctx_list.append(foreign_key)
                visible[context] = ctx_list
                tbl.annotations[_VISIBLE_FOREIGN_KEYS] = visible
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_add_visible_foreign_key",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                context=context,
            )
            return json.dumps({
                "status": "applied",
                "table": table,
                "context": context,
                "updated_list": ctx_list,
            })
        except Exception as exc:
            logger.error("add_visible_foreign_key failed: %s", exc)
            audit_event(
                "annotation_add_visible_foreign_key_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                context=context,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def remove_visible_foreign_key(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        context: str,
        foreign_key: list | int,
    ) -> str:
        """Remove a single related-table entry from the visible-foreign-keys list for one context.

        Reads the current visible-foreign-keys annotation, removes the matching entry,
        and writes the result back. Other context lists are preserved unchanged.

        Identify the entry to remove by:
            ["schema", "constraint_name"]  -- removes the first matching list entry
            integer                        -- removes by zero-based index position

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            context: Annotation context to modify, e.g. "detailed", "*".
            foreign_key: FK to remove: list [schema, constraint] or index.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
                visible = tbl.annotations.get(_VISIBLE_FOREIGN_KEYS)
                if not visible:
                    return json.dumps({"error": f"Table {table!r} has no visible-foreign-keys annotation"})
                ctx_list = list(visible.get(context) or [])
                if isinstance(foreign_key, int):
                    if 0 <= foreign_key < len(ctx_list):
                        ctx_list.pop(foreign_key)
                    else:
                        return json.dumps({"error": f"Index {foreign_key} out of range (list has {len(ctx_list)} items)"})
                else:
                    for i, item in enumerate(ctx_list):
                        if item == foreign_key:
                            ctx_list.pop(i)
                            break
                    else:
                        return json.dumps({"error": f"Foreign key {foreign_key!r} not found in context {context!r}"})
                visible = dict(visible)
                visible[context] = ctx_list
                tbl.annotations[_VISIBLE_FOREIGN_KEYS] = visible
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_remove_visible_foreign_key",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                context=context,
            )
            return json.dumps({
                "status": "applied",
                "table": table,
                "context": context,
                "updated_list": ctx_list,
            })
        except Exception as exc:
            logger.error("remove_visible_foreign_key failed: %s", exc)
            audit_event(
                "annotation_remove_visible_foreign_key_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                context=context,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def set_table_display(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        annotation: dict[str, Any] | None,
    ) -> str:
        """Set the full table-display annotation (tag:isrd.isi.edu,2016:table-display).

        Replaces the entire annotation. Pass annotation=null to remove it entirely.

        For setting only the row name pattern, use set_row_name_pattern instead.

        Table-display annotation schema:
            {
              "row_name": {
                "row_markdown_pattern": "{{{Name}}}",  // Handlebars template for row IDs
                "template_engine": "handlebars"
              },
              "compact": {
                "page_size": 25,
                "row_order": [{"column": "RCT", "descending": true}, "Name"]
              },
              "detailed": {
                "hide_column_headers": true,
                "collapse_toc_panel": true
              }
            }

        Use get_handlebars_template_variables to see available column variables.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            annotation: Full table-display annotation value, or null to remove.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                tbl = model.schemas[schema].tables[table]
                if annotation is None:
                    tbl.annotations.pop(_TABLE_DISPLAY, None)
                else:
                    tbl.annotations[_TABLE_DISPLAY] = annotation
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_set_table_display",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
            )
            return json.dumps({"status": "applied", "table": table, "annotation": "table-display"})
        except Exception as exc:
            logger.error("set_table_display failed: %s", exc)
            audit_event(
                "annotation_set_table_display_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def set_column_display(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        column: str,
        annotation: dict[str, Any] | None,
    ) -> str:
        """Set the full column-display annotation (tag:isrd.isi.edu,2016:column-display).

        Replaces the entire annotation on the column. Pass annotation=null to remove it.

        For setting only the column display name, use set_column_display_name instead.

        Column-display annotation schema:
            {
              "*": {
                "pre_format": {
                  "format": "%.2f",           // printf-style format
                  "bool_true_value": "Yes",
                  "bool_false_value": "No"
                },
                "markdown_pattern": "**{{{_value}}}**",  // Handlebars pattern
                "template_engine": "handlebars",
                "column_order": false         // disable sorting
              },
              "compact": {...},
              "detailed": {...}
            }

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
            column: Column name.
            annotation: Full column-display annotation value, or null to remove.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                model = catalog.getCatalogModel()
                col = model.schemas[schema].tables[table].columns[column]
                if annotation is None:
                    col.annotations.pop(_COLUMN_DISPLAY, None)
                else:
                    col.annotations[_COLUMN_DISPLAY] = annotation
                model.apply()
            fire_schema_change(hostname, catalog_id)
            audit_event(
                "annotation_set_column_display",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column=column,
            )
            return json.dumps({
                "status": "applied",
                "table": table,
                "column": column,
                "annotation": "column-display",
            })
        except Exception as exc:
            logger.error("set_column_display failed: %s", exc)
            audit_event(
                "annotation_set_column_display_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                table=table,
                column=column,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})