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

        See ERMREST QUERY GUIDE for path syntax, pagination, and result
        interpretation.

        Returns selected columns from an ERMREST path expression. Use this for
        multi-table joins, column projections, and cursor-based pagination.

        Args:
            hostname: DERIVA server hostname.
            catalog_id: Catalog ID, alias, or ID@snaptime (Crockford base32 --
                call resolve_snaptime to convert a date).
            path: ERMREST path relative to /attribute/ (e.g. "isa:Dataset/Status=released").
                Do NOT embed @sort/@after or trailing /* in the path.
            attributes: Columns to return. Omit for all columns.
            limit: Max rows (page size for cursor-based pagination).
            after_rid: RID of last row from previous page to advance cursor.

        Empty result sets are valid -- 0 rows means the query is correct but
        no data matches. Do NOT retry expecting different results.
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

        See ERMREST QUERY GUIDE for path syntax and result interpretation.

        Args:
            hostname: DERIVA server hostname.
            catalog_id: Catalog ID, alias, or ID@snaptime (Crockford base32 --
                call resolve_snaptime to convert a date).
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

        See ERMREST QUERY GUIDE for path syntax and aggregate expressions.

        Returns computed aggregate values over an ERMREST path expression.
        Expressions use ERMrest syntax, e.g. "cnt:=cnt(RID)", "avg_val:=avg(Age)".

        Args:
            hostname: DERIVA server hostname.
            catalog_id: Catalog ID, alias, or ID@snaptime (Crockford base32 --
                call resolve_snaptime to convert a date).
            path: ERMREST path relative to /aggregate/.
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
