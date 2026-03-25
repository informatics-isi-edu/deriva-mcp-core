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
    ) -> str:
        """Run an ERMREST attribute query.

        Returns selected columns from an ERMREST path expression. Use this for
        multi-table path traversals, column projections, and fine-grained filtering
        beyond the simple equality filters in get_entities.

        The `path` is an ERMREST path string relative to /attribute/, e.g.:
            "isa:Dataset"                      -- all rows from isa:Dataset
            "isa:Dataset/Status=released"      -- filtered rows
            "isa:Dataset/isa:Execution"        -- joined tables
            "isa:Dataset/Status=released"      -- filter only; all columns returned

        IMPORTANT: ERMrest requires a column projection after the final path
        element. When `attributes` is omitted, `/*` is appended automatically
        so all columns are returned. Do NOT include `/*` in the path yourself --
        it will be appended for you when attributes is None.

        The `attributes` list selects specific columns to return, e.g.:
            ["RID", "Name", "Status"]

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            path: ERMREST path string (without the /attribute/ prefix or trailing /*).
            attributes: Columns to return. If omitted, all columns are returned.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                url = f"/attribute/{path}"
                if attributes:
                    url += "/" + ",".join(attributes)
                else:
                    url += "/*"
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
            catalog_id: Catalog ID or alias.
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
