"""Built-in MCP prompts for DERIVA tool usage guidance.

Each prompt provides behavioral guidance, examples, and troubleshooting for
a group of related tools. Tool docstrings reference these prompts with a
"Call the '<name>' prompt before first use" directive so the LLM loads the
guidance once per conversation rather than carrying it in every tool call.

Prompts are static text -- they do not require tool calls or catalog access
to render.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..plugin.api import PluginContext


# -- Prompt content ----------------------------------------------------------

# Each constant is the full text returned as a single user message.
# Plain ASCII only in these strings (project convention).

_QUERY_GUIDE = """\
ERMREST QUERY GUIDE -- read this before using query_attribute, query_aggregate, \
or count_table.

USE THE SCHEMA CONTEXT (MANDATORY)
-----------------------------------
The system prompt contains the catalog schema including all tables, columns,
and foreign key relationships. Use that context to construct join paths.

Do NOT call get_schema, get_table, or get_table_columns to "explore" the
schema before writing a query. The information is already available to you.
Go directly to query_attribute or query_aggregate with the correct join path.

Use rag_search to look up additional ERMrest syntax details when needed.

ENDPOINTS
---------
ERMrest has four data access endpoints. The path argument in query_attribute
and query_aggregate is relative to the endpoint -- do NOT include the prefix.

    /entity/path               -- whole rows from the final table in the path
    /attribute/path/columns    -- projected columns (query_attribute uses this)
    /aggregate/path/expressions -- single-row aggregate result (query_aggregate)
    /attributegroup/path/keys;aggregates -- grouped aggregates

PATH SYNTAX
-----------
Paths are slash-separated chains of schema:table names, filters, and joins.

  Table reference:
    schema:Table                           -- all rows from one table

  Equality filter:
    schema:Table/Column=value              -- exact match

  Comparison operators (binary):
    Column::lt::value                      -- less than
    Column::leq::value                     -- less than or equal
    Column::gt::value                      -- greater than
    Column::geq::value                     -- greater than or equal

  Null test (unary):
    Column::null::                         -- column IS NULL

  Pattern matching:
    Column::regexp::pattern                -- regex match (case-sensitive)
    Column::ciregexp::pattern              -- regex match (case-insensitive)
    Column::ts::query                      -- full-text search

  Negation:
    !Column=value                          -- NOT equal
    !Column::null::                        -- column IS NOT NULL

  Conjunction and disjunction within a single filter element:
    Column1=val1&Column2=val2              -- AND (& separator)
    Column1=val1;Column2=val2              -- OR  (; separator)
    Precedence: & binds tighter than ;

  Quantified value lists:
    Column=any(val1,val2,val3)             -- match any value (IN)
    Column=all(val1,val2,val3)             -- match all values

  Successive path elements are implicitly ANDed:
    schema:Table/Col1=a/Col2=b             -- Col1=a AND Col2=b

JOINS (FOREIGN KEY TRAVERSAL)
-----------------------------
Add a table name after a path element to traverse a foreign key relationship.
ERMrest resolves the FK automatically when there is exactly one FK between the
two tables. If there are multiple FKs, disambiguate with column mapping syntax.

  Simple join (unambiguous FK):
    schema:A/schema:B                      -- join A to B via the FK between them

  Filtered join:
    schema:A/Status=released/schema:B      -- filter A first, then join to B

  Multi-hop join:
    schema:A/schema:B/schema:C             -- A -> B -> C

  Explicit column mapping (when FK is ambiguous):
    schema:A/(FK_Column)=(schema:B:PK_Column)

  Table instance aliases (for self-joins or referring back):
    a:=schema:Table                        -- bind alias "a" to Table
    $a/Column=value                        -- refer back to alias "a"

MULTI-HOP JOIN EXAMPLES
-----------------------
IMPORTANT: When the user asks for data across related tables, always use
query_attribute with a join path. Do NOT call get_entities on each table
separately and try to manually correlate the results.

NOTE: The schema and table names below are ILLUSTRATIVE ONLY. Every catalog
has different schema names. Use the schema information from the system prompt
to determine the correct schema:Table names for your catalog.

  Multi-hop join (schema and table names will vary by catalog):
    path="MySchema:Study/RID=16-2CNW/MySchema:Experiment/MySchema:File"

  Filter on a column:
    path="MySchema:Dataset/Status=released"

  Count with aggregate:
    Use query_aggregate with path="MySchema:Study/RID=abc/MySchema:Experiment"
    and aggregates=["cnt:=cnt(RID)"]

  Aliases for columns from multiple tables:
    path="s:=MySchema:Study/e:=MySchema:Experiment"
    attributes=["s:Name", "e:RID", "e:Name"]

COLUMN PROJECTION (query_attribute only)
----------------------------------------
The `attributes` parameter selects columns to return. When omitted, all columns
are returned (/* is appended automatically -- do NOT add it yourself).

    attributes=["RID", "Name", "Status"]   -- specific columns
    attributes=None                        -- all columns (default)

Columns from aliased tables: "alias:ColumnName"
Column aliases in the URL: "out_name:=ColumnName"

IMPORTANT: ERMrest does NOT support schema:table:column syntax in attributes.
If your path spans multiple tables and you need columns from a non-final table,
you MUST assign aliases in the path and use alias:Column in attributes.

  WRONG (will fail with 400, ILLUSTRATIVE ONLY):
    path="Data:Study/Data:Experiment/Data:File"
    attributes=["Data:Study:RID", "Data:Study:Title"]

  CORRECT (ILLUSTRATIVE ONLY):
    path="s:=Data:Study/Data:Experiment/Data:File"
    attributes=["s:RID", "s:Title"]

PAGINATION
----------
Use cursor-based pagination with `limit` and `after_rid`:

    Page 1: limit=50                       (no after_rid)
    Page 2: limit=50, after_rid="<RID of last row from page 1>"
    Page 3: limit=50, after_rid="<RID of last row from page 2>"
    Stop when: returned count < limit

IMPORTANT: @sort and @after must appear AFTER the column projection in ERMrest
attribute URLs. Always use the `after_rid` and `limit` parameters rather than
embedding @sort/@after in the path string directly.

URL ENCODING
------------
Values in filter predicates must be URL-encoded. Special characters:
    space -> %20     / -> %2F     : -> %3A     = -> %3D
    & -> %26         ; -> %3B     ( -> %28     ) -> %29
Timestamp values: "2024-01-15T12:00:00" (use T separator, URL-encode if needed).
Boolean values: true, false (lowercase).

EMPTY RESULTS ARE VALID (MANDATORY)
------------------------------------
An empty result set means the path is syntactically correct but no rows match.

Apply these rules strictly when a query returns 0 rows:

1. Zero rows is a COMPLETE ANSWER. Report it to the user immediately.
2. Do NOT reformulate the query with different joins hoping for results.
3. Do NOT break the query into smaller queries on individual tables.
4. Do NOT call count_table or get_entities on intermediate tables to
   "investigate" why there are no results.
5. Do NOT assume you made a mistake -- if there was no HTTP error, the
   query was correct and the data simply does not exist.

The single most common error is refusing to accept 0 rows. A query that
returns 0 rows followed by 5-10 "investigative" calls wastes the user's
time and money. Stop after the first query and report the finding.

Distinguish between:
- HTTP error from ERMrest: the query syntax is wrong -- fix the path
- Empty result set (0 rows): the query is correct, the data does not exist

A path can return 0 rows because:
- No rows match the filter criteria
- The FK relationship exists but no rows are linked across tables
- Individual tables have data but those specific rows are not connected

AGGREGATE EXPRESSIONS
---------------------
For query_aggregate, expressions follow ERMrest syntax:

    "cnt:=cnt(RID)"       -- row count
    "cnt_d:=cnt_d(Col)"   -- count distinct
    "avg_val:=avg(Score)"  -- average
    "sum_val:=sum(Score)"  -- sum
    "max_ts:=max(RCT)"    -- maximum value
    "min_id:=min(RID)"    -- minimum value
    "vals:=array(Col)"    -- collect into array
    "vals:=array_d(Col)"  -- collect distinct into array

DISPLAY RULES (MANDATORY)
-------------------------
Before rendering any query result, apply these rules strictly:

1. ALWAYS include the RID column.
2. REMOVE only the four system columns: RCT, RMT, RCB, RMB.
3. DISPLAY EVERY OTHER COLUMN from the result set -- no exceptions.
   Do NOT drop a column because its values are all null.
   Do NOT drop a column because you think it is unimportant.
   Do NOT summarize or abbreviate the column list for readability.
   The user expects to see the complete schema of the result.

Violation of rule 3 is the single most common display error. If you are
tempted to hide a column, do not -- show it.

CATALOG SNAPTIME
----------------
catalog_id can be a compound "ID@snaptime" for historical snapshot access.
The snaptime must be a Crockford base32 string (e.g. "2TA-YA2D-ZDWY"), never
a plain date. Call resolve_snaptime first to convert a human-readable date.
"""

_ENTITY_GUIDE = """\
ENTITY TOOL GUIDE -- read this before using get_entities, insert_entities, \
update_entities, or delete_entities.

USE THE SCHEMA CONTEXT -- DO NOT EXPLORE
-----------------------------------------
The system prompt already contains the catalog schema with all tables, columns,
and foreign key relationships. Do NOT call get_schema, get_table, or
get_table_columns to "understand the structure" before querying. Construct
your join path directly from the schema context you already have.

STOP -- USE THE RIGHT TOOL FOR MULTI-TABLE QUERIES
---------------------------------------------------
get_entities retrieves rows from ONE table only. If the user asks for data
that spans related tables (e.g. "files for a study", "experiments in a
project", "samples linked to a dataset"), do NOT call get_entities on each
table separately and try to stitch the results together. That approach is
slow, error-prone, and often fails.

Instead, use query_attribute with a join path that traverses the foreign key
chain in a single query. ERMrest resolves FK joins automatically.

Example -- find all files linked to a specific study (schema and table names
are illustrative -- use the actual names from the system prompt schema):

    query_attribute(
        path="MySchema:Study/RID=16-2CNW/MySchema:Experiment/MySchema:File",
        attributes=["RID", "URL", "Caption", "MD5"],
        limit=100,
    )

This single call replaces what would otherwise be 3+ separate get_entities
calls. See the ERMREST QUERY GUIDE for full join syntax.

PREFLIGHT COUNT RULE (MANDATORY)
--------------------------------
When the row count of the target table is not already known from a prior
count_table or get_entities preflight call in this conversation, you MUST call
get_entities with preflight_count=True before fetching any rows.

This is mandatory whenever the user asks to retrieve "all" records, an
unfiltered table, or any table whose size has not been established.

When preflight_count=True:
- The tool ONLY returns the row count -- it never fetches entities
- Present the count to the user
- Ask whether to proceed and with what limit
- Then call again with preflight_count=False to actually retrieve rows

The preflight flag is False by default so repeat calls (where the count is
already known) do not pay the extra round-trip.

CURSOR-BASED PAGINATION
------------------------
Rows are always returned sorted by RID. To retrieve subsequent pages:

    Page 1: get_entities(schema, table, limit=50)
    Page 2: get_entities(schema, table, limit=50, after_rid="LAST_RID")
    Page 3: get_entities(schema, table, limit=50, after_rid="LAST_RID")

Replace LAST_RID with the RID of the last row from the previous page.
Stop when count < limit.

CHOOSING THE RIGHT TOOL
------------------------
get_entities is for whole-row retrieval from a single table. For other patterns:
- Column projection (select specific columns): query_attribute
- Path traversal across joined tables: query_attribute
- Aggregate functions (count, avg, max): query_aggregate
- Row count with optional filters: count_table
- Single record by RID: use filters={"RID": rid}

INSERT ENTITIES
---------------
- Omit RID and system columns (RCT, RMT, RCB, RMB) -- ERMrest generates them
- The tool uses the defaults mechanism so omitted nullable columns get defaults
- Returns the inserted rows including generated RIDs

UPDATE ENTITIES
---------------
- Uses PUT /attributegroup for sparse updates -- only specified columns change
- Each entity dict must include an RID to identify the target row
- Only the columns present in the update dict are modified

DELETE ENTITIES
---------------
- Requires non-empty filters for safety -- will not delete all rows
- Use filters={"RID": rid} to delete a single specific row

DISPLAY RULES (MANDATORY)
-------------------------
Before rendering any query result, apply these rules strictly:

1. ALWAYS include the RID column.
2. REMOVE only the four system columns: RCT, RMT, RCB, RMB.
3. DISPLAY EVERY OTHER COLUMN from the result set -- no exceptions.
   Do NOT drop a column because its values are all null.
   Do NOT drop a column because you think it is unimportant.
   Do NOT drop a column because the values are long or wide.
   Do NOT summarize or abbreviate the column list for readability.
   The user expects to see the complete schema of the result.

Violation of rule 3 is the single most common display error. If you are
tempted to hide a column, do not -- show it.

CATALOG SNAPTIME
----------------
catalog_id can be a compound "ID@snaptime" for historical snapshot access.
The snaptime must be a Crockford base32 string -- never a plain date.
Call resolve_snaptime first to convert a human-readable date.
"""

_ANNOTATION_GUIDE = """\
ANNOTATION TOOL GUIDE -- read this before using annotation tools.

ANNOTATION OVERVIEW
-------------------
DERIVA annotations are JSON objects stored on catalog objects (tables, columns,
foreign keys, schemas) that control how the Chaise web UI displays and interacts
with data. The annotation tools let you read and modify these display settings.

COMMON WORKFLOWS
----------------

1. Change how a table is displayed:
   - get_table_annotations to see current settings
   - set_display_annotation for name_style, title, markdown_pattern
   - set_row_name_pattern to control how rows are labeled in the UI
   - set_visible_columns to control which columns appear and in what order
   - set_visible_foreign_keys to control which related tables are shown

2. Customize column appearance:
   - set_column_display_name for a user-friendly column label
   - set_column_display to control per-context rendering (e.g. markdown)

3. Configure the Chaise navbar:
   - apply_navbar_annotations sets chaise-config and catalog display at once
   - Accepts navbar menu items, default catalog settings, headTitle

CONTEXT NAMES
-------------
Many annotation tools accept a `context` parameter that controls where the
setting applies in the Chaise UI:

    "compact"          -- table listing / search results
    "compact/brief"    -- inline/embedded table listings
    "compact/select"   -- foreign key picker dropdowns
    "detailed"         -- single-record detail page
    "entry"            -- record creation form
    "entry/edit"       -- record edit form
    "*"                -- all contexts (default/fallback)

COLUMN DIRECTIVES (visible_columns)
------------------------------------
Each entry in a visible_columns list can be:
- A plain column name string: "Name"
- A source path array: [{"outbound": ["schema", "fk_name"]}, "RID"]
- An object with display options: {"source": "Name", "markdown_name": "**Name**"}

FOREIGN KEY DIRECTIVES (visible_foreign_keys)
----------------------------------------------
Each entry in visible_foreign_keys is an object with a "source" array:
    {"source": [{"inbound": ["schema", "fk_name"]}, "RID"]}

HANDLEBARS TEMPLATES
--------------------
Several annotations use Handlebars template syntax for dynamic text:
    "Name: {{Name}}, ID: {{RID}}"

Use get_handlebars_template_variables to discover available variables for a
table before writing templates. Use validate_template_syntax to check for
common errors. Use preview_handlebars_template to test rendering against
sample data.

IMPORTANT: Always validate template syntax before setting annotations that
use Handlebars patterns (row_name, markdown_pattern, column_display).
"""

_CATALOG_GUIDE = """\
CATALOG TOOL GUIDE -- read this before using catalog management tools.

SNAPTIME FORMAT
---------------
ERMrest snapshots are identified by Crockford base32 timestamps, not ISO dates.

    Crockford base32: "2TA-YA2D-ZDWY"   (what ERMrest expects)
    ISO date:         "2024-01-15"        (NOT valid as a snaptime)

Always call resolve_snaptime to convert a human-readable date to the correct
format before using catalog_id@snaptime compound identifiers.

resolve_snaptime accepts:
- ISO date: "2024-01-15"
- ISO datetime: "2024-01-15T10:30:00"
- Relative: "yesterday", "last week"
- Crockford base32 (passed through): "2TA-YA2D-ZDWY"

The tool returns the canonical snaptime plus the resolved UTC timestamp.

CATALOG CLONING
---------------
clone_catalog creates an exact copy of a catalog's schema and data. Key points:
- The clone protocol is idempotent -- interrupted clones can be resumed
- Use clone_catalog_async for large catalogs (returns a background task ID)
- Poll the task via get_task_status; the clone may take minutes for large catalogs
- The destination catalog must exist (create it first with create_catalog)

CATALOG ALIASES
---------------
Aliases provide stable, human-readable identifiers for catalogs:
- Create: create_catalog_alias(hostname, alias_name, catalog_id)
- The alias can then be used anywhere a catalog_id is accepted
- Useful for "latest", "production", "staging" patterns

HISTORY BOUNDS
--------------
get_catalog_history_bounds returns the earliest and latest snapshot identifiers
known to the catalog. Use this to determine the available time range before
querying historical snapshots.
"""


# -- Registration ------------------------------------------------------------


def register(ctx: PluginContext) -> None:
    """Register built-in prompts with the MCP server."""

    @ctx.prompt("query_guide",
                description="ERMrest query patterns, path syntax, pagination, and result interpretation")
    def query_guide() -> str:
        return _QUERY_GUIDE

    @ctx.prompt("entity_guide",
                description="Entity CRUD patterns, preflight count rule, pagination, and display rules")
    def entity_guide() -> str:
        return _ENTITY_GUIDE

    @ctx.prompt("annotation_guide",
                description="Chaise annotation patterns, context names, column/FK directives, and Handlebars templates")
    def annotation_guide() -> str:
        return _ANNOTATION_GUIDE

    @ctx.prompt("catalog_guide",
                description="Catalog management, snaptime format, cloning, aliases, and history")
    def catalog_guide() -> str:
        return _CATALOG_GUIDE
