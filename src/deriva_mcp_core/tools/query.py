from __future__ import annotations

"""Attribute and aggregate query tools for DERIVA catalogs.

Provides MCP tools for ERMRest query operations:
    query_attribute   -- Attribute query returning projected columns from a path
    query_aggregate   -- Aggregate query returning computed values over a path
"""

import json
import logging
from typing import TYPE_CHECKING

from ..context import deriva_call, get_catalog

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)


def register(ctx: PluginContext) -> None:
    """Register query tools with the MCP server."""

    @ctx.tool(mutates=False)
    async def query_attribute(
        hostname: str,
        catalog_id: str,
        path: str,
        attributes: list[str] | None = None,
        limit: int | None = None,
        after_rid: str | None = None,
    ) -> str:
        """Run an ERMREST attribute query.

        Returns selected columns from an ERMREST path expression. Use this for
        multi-table path traversals, column projections, fine-grained filtering,
        and cursor-based pagination beyond get_entities.

        The `path` is an ERMREST path string relative to /attribute/, e.g.:
            "isa:Dataset"                      -- all rows from isa:Dataset
            "isa:Dataset/Status=released"      -- filtered rows
            "isa:Dataset/isa:Execution"        -- joined tables

        IMPORTANT: ERMrest requires a column projection after the final path
        element. When `attributes` is omitted, `/*` is appended automatically
        so all columns are returned. Do NOT include `/*` in the path yourself --
        it will be appended for you when attributes is None.

        The `attributes` list selects specific columns to return, e.g.:
            ["RID", "Name", "Status"]

        Cursor-based pagination for large result sets:
            ERMrest RIDs are monotonically assigned, so @sort(RID)@after(last_rid)
            provides stable cursor-based pagination. Use `limit` to set page size
            and `after_rid` to advance the cursor to the next page.

            IMPORTANT: @sort and @after must appear AFTER the column projection in
            ERMrest attribute URLs. Always use the after_rid and limit parameters
            rather than embedding @sort/@after in the path string directly.

                Page 1: path="isa:Dataset", limit=50              (no after_rid)
                Page 2: path="isa:Dataset", limit=50, after_rid="LAST_RID"
                Page 3: path="isa:Dataset", limit=50, after_rid="LAST_RID"

            Replace LAST_RID with the RID of the last row from the previous page.
            Use the same path and attributes on every page. Stop when count < limit.
            For simple whole-entity pagination (no column projection or joins),
            get_entities also supports after_rid directly.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID, alias, or compound ID@snaptime for historical
                snapshot access. The snaptime must be a Crockford base32 string --
                never a plain date. Call resolve_snaptime first to convert a date.
            path: ERMREST path string (without the /attribute/ prefix or trailing /*).
                Do NOT embed @sort or @after in the path; use the dedicated params.
            attributes: Columns to return. If omitted, all columns are returned.
            limit: Maximum rows to return (passed as ?limit=N query parameter).
                Use this to control page size for cursor-based pagination.
            after_rid: When set, adds @sort(RID)@after(after_rid) after the column
                projection to advance the cursor to the next page. Set this to the
                RID of the last row from the previous page.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                url = f"/attribute/{path}"
                if attributes:
                    url += "/" + ",".join(attributes)
                else:
                    url += "/*"
                # @sort/@after must come after the column projection in ERMrest
                # attribute URLs, then limit is a query parameter.
                if after_rid is not None:
                    url += f"@sort(RID)@after({after_rid})"
                if limit is not None:
                    url += f"?limit={limit}"
                rows = catalog.get(url).json()
                return json.dumps(
                    {
                        "path": path,
                        "attributes": attributes,
                        "count": len(rows),
                        "rows": rows,
                    }
                )
        except Exception as exc:
            logger.error("query_attribute failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def count_table(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        filters: dict | None = None,
    ) -> str:
        """Count rows in a table, with optional equality filters.

        Shorthand for a single-aggregate ERMrest query that returns
        the row count as a plain integer. Equivalent to calling
        query_aggregate with "cnt:=cnt(RID)" but with a simpler interface.

        Optional `filters` is a dict of {column: value} equality constraints.
        Each pair is appended to the URL as an ERMrest filter predicate.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID, alias, or compound ID@snaptime for historical
                snapshot access. The snaptime must be a Crockford base32 string --
                never a plain date. Call resolve_snaptime first to convert a date.
            schema: Schema name.
            table: Table name.
            filters: Optional equality filters, e.g. {"Status": "released"}.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                filter_seg = ""
                if filters:
                    filter_seg = "".join(f"/{k}={v}" for k, v in filters.items())
                url = f"/aggregate/{schema}:{table}{filter_seg}/cnt:=cnt(RID)"
                result = catalog.get(url).json()
                count = result[0]["cnt"] if result else 0
                return json.dumps(
                    {
                        "schema": schema,
                        "table": table,
                        "filters": filters,
                        "count": count,
                    }
                )
        except Exception as exc:
            logger.error("count_table failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def query_aggregate(
        hostname: str,
        catalog_id: str,
        path: str,
        aggregates: list[str],
    ) -> str:
        """Run an ERMREST aggregate query.

        Returns computed aggregate values over an ERMREST path expression.
        Aggregate expressions follow ERMREST syntax:
            "cnt:=cnt(RID)"        -- row count
            "avg_age:=avg(Age)"    -- average of Age column
            "max_ts:=max(RCT)"     -- maximum value

        The `path` is an ERMREST path string relative to /aggregate/, e.g.:
            "isa:Dataset"
            "isa:Dataset/Status=released"

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID, alias, or compound ID@snaptime for historical
                snapshot access. The snaptime must be a Crockford base32 string --
                never a plain date. Call resolve_snaptime first to convert a date.
            path: ERMREST path string (without the /aggregate/ prefix).
            aggregates: List of aggregate expressions.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                url = f"/aggregate/{path}/{','.join(aggregates)}"
                result = catalog.get(url).json()
                return json.dumps(
                    {
                        "path": path,
                        "aggregates": aggregates,
                        "result": result,
                    }
                )
        except Exception as exc:
            logger.error("query_aggregate failed: %s", exc)
            return json.dumps({"error": str(exc)})
