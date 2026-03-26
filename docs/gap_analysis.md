# Gap Analysis: deriva-mcp prototype vs deriva-mcp-core

**Date:** 2026-03-25
**Prototype:** `C:\src\isrd\deriva-mcp` (DerivaML MCP Server)
**New implementation:** `C:\src\isrd\deriva-mcp-core` (DERIVA MCP Core)

This document provides a function-by-function parity analysis across every tool module in both
codebases, a gap analysis of what is in the prototype but absent from core, and an addendum
covering what core has introduced that the prototype never had.

---

## Table of Contents

1. [Scope and Architecture Differences](#1-scope-and-architecture-differences)
2. [Parity Analysis by Module](#2-parity-analysis-by-module)
   - 2.1 Catalog and Connection Management
   - 2.2 Schema Introspection (catalog.py)
   - 2.3 Entity / Data Access
   - 2.4 Annotation Tools
   - 2.5 Vocabulary Tools
   - 2.6 Schema DDL Tools
   - 2.7 RAG Tools
   - 2.8 Hatrac Object Store
3. [Gap Analysis -- In Prototype, Absent from Core](#3-gap-analysis----in-prototype-absent-from-core)
   - 3.1 Dataset Tools (all absent)
   - 3.2 Execution Tools (all absent)
   - 3.3 Feature Tools (all absent)
   - 3.4 Workflow Tools (all absent)
   - 3.5 Background Task Tools (all absent)
   - 3.6 Developer Tools (all absent)
   - 3.7 Catalog Lifecycle Tools
   - 3.8 Vocabulary Gaps
   - 3.9 Annotation Gaps
   - 3.10 Data Access Gaps
   - 3.11 RAG Tool Gaps
   - 3.12 Resources and Prompts (all absent)
4. [Addendum -- In Core, Absent from Prototype](#4-addendum----in-core-absent-from-prototype)

---

## 1. Scope and Architecture Differences

Before the function-level analysis, the following architectural differences must be
understood because they affect how parity should be evaluated.

### Connection model

The prototype uses a `ConnectionManager` that maintains an explicit, named connection
(keyed by `user_id + hostname + catalog_id`). Tools call `conn_manager.get_active_or_raise()`
and operate on a `DerivaML` instance stored in that connection. An explicit
`connect_catalog` / `disconnect_catalog` lifecycle exists.

Core is stateless. There is no connection object. Every tool call receives `hostname`
and `catalog_id` as explicit parameters. Authentication is per-request via OAuth token
(HTTP) or disk credential (stdio). The closest equivalent to "connect" is
`get_catalog(hostname, catalog_id)` which is called inline by each tool.

This means the prototype's `connect_catalog`, `disconnect_catalog`, `set_active_catalog`,
and `set_default_schema` have no direct counterparts in core by design, not by omission.

### DerivaML dependency

The prototype depends on `deriva-ml`, a high-level library that provides Dataset,
Execution, Workflow, and Feature abstractions on top of ERMrest. Core depends only on
`deriva` (the lower-level library). All prototype tools that delegate to `deriva-ml`
(`ml.create_dataset()`, `ml.create_execution()`, `ml.create_feature()`, etc.) cannot
be ported to core without either re-importing `deriva-ml` or reimplementing the
equivalent logic.

The planned resolution is a `deriva-ml` plugin that runs on top of core and re-exposes
this functionality using the plugin framework.

### Annotation staging model

The prototype stages annotation changes locally and requires an explicit
`apply_annotations()` call to commit them. Core applies every annotation change
immediately via `model.apply()`. There is no staged-commit model.

### Tool naming: schema_name vs implicit default

Prototype tools that target a table accept just `table_name` and rely on
`ml.model.name_to_table()` to resolve the schema from the active connection's
`default_schema`. Core requires an explicit `schema` parameter on most tools because
there is no connection state to consult.

---

## 2. Parity Analysis by Module

### 2.1 Catalog and Connection Management

| Prototype tool | Core equivalent | Notes |
|---|---|---|
| `connect_catalog(hostname, catalog_id, domain_schema, default_schema)` | None -- by design | Stateless architecture; no explicit connect step |
| `disconnect_catalog()` | None -- by design | No connection to disconnect |
| `set_active_catalog(hostname, catalog_id)` | None -- by design | No session concept in core |
| `set_default_schema(schema_name)` | None -- by design | Schema passed explicitly on every call |
| `create_catalog(hostname, project_name, catalog_alias)` | None | Creates new catalog with full ML schema via `deriva-ml`; no core equivalent |
| `delete_catalog(hostname, catalog_id)` | None | No core equivalent |
| `apply_catalog_annotations(navbar_brand_text, head_title)` | None | Chaise nav-bar/display setup; no core equivalent |
| `create_catalog_alias(hostname, alias_name, catalog_id, ...)` | None | No core equivalent |
| `update_catalog_alias(hostname, alias_name, ...)` | None | No core equivalent |
| `delete_catalog_alias(hostname, alias_name)` | None | No core equivalent |
| `clone_catalog(source_hostname, source_catalog_id, root_rid, ...)` | None | `create_ml_workspace` wrapper; no core equivalent |
| `validate_rids(dataset_rids, asset_rids, ...)` | None | Batch RID existence check; no core equivalent |
| `cite(rid, current)` | None | Citation URL generation; no core equivalent |

### 2.2 Schema Introspection (catalog.py)

| Prototype tool | Core equivalent | Notes |
|---|---|---|
| None | `get_catalog_info(hostname, catalog_id)` | Core only -- returns catalog-level metadata |
| None | `list_schemas(hostname, catalog_id)` | Core only -- lists schema names |
| None | `get_schema(hostname, catalog_id, schema)` | Core only -- full schema with tables, columns, FK summaries |
| `connect_catalog` fires schema fetch on connect | `get_table(hostname, catalog_id, schema, table)` | Core exposes this as an explicit tool; prototype schema info only accessible via resources |

The prototype has no dedicated schema-introspection tools. Schema information was
surfaced through MCP resources (`deriva://catalog/schema`) and through the `DerivaML`
model object accessed implicitly by other tools. Core provides explicit tools and
fires `on_catalog_connect` to trigger RAG indexing.

### 2.3 Entity / Data Access

| Prototype tool | Core equivalent | Notes |
|---|---|---|
| `get_table(table_name, limit)` | `get_entities(hostname, catalog_id, schema, table, filters)` | Core requires explicit schema; also supports filters |
| `query_table(table_name, columns, filters, limit, offset)` | `get_entities(...)` | Core does not support column projection; uses limit capped at 1000 |
| `count_table(table_name, filters)` | `query_aggregate(hostname, catalog_id, path, aggregates)` | Core equivalent uses ERMrest aggregate path directly |
| `insert_records(table_name, records)` | `insert_entities(hostname, catalog_id, schema, table, entities)` | Parity; core adds `defaults` passthrough |
| `get_record(table_name, rid)` | `get_entities(...)` with filter `RID=rid` | No dedicated shorthand in core |
| `update_record(table_name, rid, updates)` | `update_entities(hostname, catalog_id, schema, table, entities)` | Core uses sparse `PUT /attributegroup`; prototype used `path.update()` with full record |
| None | `query_attribute(hostname, catalog_id, path, attributes)` | Core only -- arbitrary ERMrest attribute path |
| None | `query_aggregate(hostname, catalog_id, path, aggregates)` | Core only -- aggregate queries |
| None | `delete_entities(hostname, catalog_id, schema, table, filters)` | Core only -- no equivalent in prototype (deletion was managed-table-only) |

**Gaps in core vs prototype:**
- No `count_table` shorthand (requires building an aggregate query)
- No `get_record` by RID shorthand
- No column projection in `get_entities` (prototype's `columns` param)
- No `offset` pagination in `get_entities` (prototype supported offset)

**Prototype restrictions absent from core:**
- Prototype `insert_records` blocked insertion into "managed" tables (Dataset, Execution,
  Workflow, etc.) with explicit error messages directing to the correct tool. Core has no
  such guardrails because those managed-table abstractions do not exist in core.

### 2.4 Annotation Tools

| Prototype tool | Core equivalent | Notes |
|---|---|---|
| `set_display_annotation(table_name, column_name, annotation)` | `set_display_annotation(hostname, catalog_id, schema, table, column, annotation)` | Parity; core requires explicit schema, applies immediately |
| `set_visible_columns(table_name, context, columns)` | `set_visible_columns(hostname, catalog_id, schema, table, context, columns)` | Parity |
| `add_visible_column(table_name, context, column, position)` | `add_visible_column(...)` | Parity |
| `remove_visible_column(table_name, context, column)` | `remove_visible_column(...)` | Parity |
| `set_visible_foreign_keys(table_name, context, fkeys)` | `set_visible_foreign_keys(...)` | Parity |
| `add_visible_foreign_key(table_name, context, fkey, position)` | `add_visible_foreign_key(...)` | Parity |
| `remove_visible_foreign_key(table_name, context, fkey)` | `remove_visible_foreign_key(...)` | Parity |
| `set_table_display(table_name, context_settings)` | `set_table_display(...)` | Parity |
| `set_column_display(table_name, column_name, context_settings)` | `set_column_display(...)` | Parity |
| `get_handlebars_template_variables(table_name)` | `get_handlebars_template_variables(hostname, catalog_id, schema, table)` | Parity |
| `apply_annotations()` | None | Staging model not present in core; core applies immediately |
| `reorder_visible_columns(table_name, context, new_order)` | None | Not implemented in core |
| `reorder_visible_foreign_keys(table_name, context, new_order)` | None | Not implemented in core |
| `get_table_sample_data(table_name, limit)` | None | Sample row fetch for template testing; not in core |
| `preview_handlebars_template(template, data)` | None | Template rendering preview; not in core |
| `validate_template_syntax(template)` | None | Handlebars syntax validation; not in core |
| None | `get_table_annotations(hostname, catalog_id, schema, table)` | Core only -- read all annotations on a table |
| None | `get_column_annotations(hostname, catalog_id, schema, table, column)` | Core only -- read all annotations on a column |
| None | `list_foreign_keys(hostname, catalog_id, schema, table)` | Core only -- list FKs with their annotations |

**Key architectural difference:** The prototype also places `set_table_display_name` and
`set_row_name_pattern` in `schema.py` rather than `annotation.py`. Core places all
annotation-touching tools in `annotation.py`. The tools exist in both; only the module
grouping differs.

### 2.5 Vocabulary Tools

| Prototype tool | Core equivalent | Notes |
|---|---|---|
| `add_term(vocabulary_name, term_name, description, synonyms)` | `add_term(hostname, catalog_id, schema, vocabulary, name, description, synonyms)` | Parity; core requires explicit hostname/catalog/schema |
| `delete_term(vocabulary_name, term_name)` | `delete_term(...)` | Parity |
| `create_vocabulary(vocabulary_name, comment, schema)` | None | Create new vocabulary table; not in core |
| `add_synonym(vocabulary_name, term_name, synonym)` | Part of `update_term(...)` | Core consolidates into update_term |
| `remove_synonym(vocabulary_name, term_name, synonym)` | Part of `update_term(...)` | Core consolidates into update_term |
| `update_term_description(vocabulary_name, term_name, description)` | Part of `update_term(...)` | Core consolidates into update_term |
| None | `list_vocabulary_terms(hostname, catalog_id, schema, vocabulary)` | Core only -- list all terms |
| None | `lookup_term(hostname, catalog_id, schema, vocabulary, name)` | Core only -- find by name or synonym |
| None | `update_term(hostname, catalog_id, schema, vocabulary, rid, name, description, synonyms)` | Core only -- consolidated update |

**Gap:** `create_vocabulary` exists in the prototype (creates the underlying vocabulary
table with standard columns) but is absent from core. This is a meaningful gap for
catalog setup workflows.

### 2.6 Schema DDL Tools

| Prototype tool | Core equivalent | Notes |
|---|---|---|
| `create_table(table_name, columns, comment, ...)` | `create_table(hostname, catalog_id, schema, table, columns, comment, ...)` | Near parity; core requires schema parameter |
| `add_column(table_name, column_name, type, ...)` | `add_column(hostname, catalog_id, schema, table, column, type, ...)` | Near parity |
| `set_table_description(table_name, description)` | `set_table_description(hostname, catalog_id, schema, table, description)` | Parity |
| `set_column_description(table_name, column_name, description)` | `set_column_description(...)` | Parity |
| `set_column_nullok(table_name, column_name, nullok)` | `set_column_nullok(...)` | Parity |
| `set_table_display_name(table_name, display_name)` | `set_table_display_name(...)` (in annotation.py) | Parity; different module in core |
| `set_row_name_pattern(table_name, pattern)` | `set_row_name_pattern(...)` (in annotation.py) | Parity; different module in core |
| `set_column_display_name(table_name, column_name, display_name)` | `set_column_display_name(...)` (in annotation.py) | Parity; different module in core |
| `create_asset_table(table_name, comment, ...)` | None | Creates DerivaML-convention asset table; no core equivalent |
| `list_asset_executions(asset_rid, asset_role)` | None | Lists executions linked to an asset; no core equivalent |
| `add_asset_type(type_name, description)` | None | Adds term to Asset_Type vocabulary; no core equivalent |
| `add_asset_type_to_asset(asset_rid, type_name)` | None | Tags an asset with a type; no core equivalent |
| `remove_asset_type_from_asset(asset_rid, type_name)` | None | Removes type tag from asset; no core equivalent |

**Note:** The three `*_asset_type*` tools and `create_asset_table` depend on DerivaML
conventions (an `Asset_Type` vocabulary table, an asset table structure with specific
FK patterns). They belong in a `deriva-ml` plugin rather than in core.

### 2.7 RAG Tools

| Prototype tool | Core equivalent | Notes |
|---|---|---|
| `rag_search(query, limit, source, doc_type, include_schema, include_data)` | `rag_search(query, limit, hostname, catalog_id, doc_type)` | Near parity; different parameter shapes for catalog scoping |
| `rag_ingest(source_name)` | None | Full re-crawl; core has no equivalent (auto-update at startup covers most use cases) |
| `rag_update(source_name)` | `rag_update_docs(source_name)` | Parity -- incremental SHA-delta update |
| `rag_status()` | `rag_status()` | Parity |
| `rag_add_source(name, repo_owner, repo_name, branch, path_prefix, ...)` | None | Runtime source addition; core uses plugin `ctx.rag_source()` for this |
| `rag_remove_source(name)` | None | No equivalent in core |
| `rag_index_schema()` | `rag_index_schema(hostname, catalog_id)` | Prototype uses active connection; core takes explicit params |
| None | `rag_index_table(hostname, catalog_id, schema, table)` | Core only -- per-user table data indexing |

**Key difference:** Prototype `rag_ingest` and `rag_update` dispatch work as background
tasks via `BackgroundTaskManager` and return a `task_id` for progress polling. Core's
`rag_update_docs` runs inline (awaited). Core has no background task infrastructure, so
long-running operations complete synchronously within the tool call or could time out.

**Key difference:** Prototype `rag_add_source` persists to `sources.json` and allows
runtime operator management of sources. Core sources are declared statically via
`ctx.rag_source()` in plugin `register()` functions or baked into the built-in source
list. There is no runtime add/remove mechanism.

### 2.8 Hatrac Object Store

| Prototype tool | Core equivalent | Notes |
|---|---|---|
| None | `list_namespace(hostname, path)` | Core only |
| None | `get_object_metadata(hostname, path)` | Core only |
| None | `create_namespace(hostname, path)` | Core only |

The prototype has no Hatrac tools. Any Hatrac access in the prototype required direct
Python use of `deriva-ml`'s internal Hatrac client, which is not exposed as an MCP tool.

---

## 3. Gap Analysis -- In Prototype, Absent from Core

### 3.1 Dataset Tools (all absent)

These 23 tools implement the DerivaML Dataset abstraction: versioned, reproducible
collections of domain records. All require `deriva-ml` and belong in a `deriva-ml` plugin.

| Tool | Prototype signature | Description |
|---|---|---|
| `create_dataset` | `(description, dataset_types, version)` | Create empty dataset within active execution |
| `get_dataset_spec` | `(dataset_rid, version)` | Get dataset specification with version info |
| `list_dataset_members` | `(dataset_rid, table_name, limit)` | List records belonging to dataset |
| `add_dataset_members` | `(dataset_rid, table_name, rids, filters)` | Add records to dataset via RIDs or filters |
| `delete_dataset_members` | `(dataset_rid, table_name, rids)` | Remove records from dataset |
| `increment_dataset_version` | `(dataset_rid, bump_type, description)` | Bump semver on dataset |
| `delete_dataset` | `(dataset_rid, recurse)` | Delete dataset and optionally its children |
| `set_dataset_description` | `(dataset_rid, description)` | Update dataset description |
| `add_dataset_type` | `(dataset_rid, dataset_type)` | Tag dataset with type label |
| `remove_dataset_type` | `(dataset_rid, dataset_type)` | Remove type label |
| `add_dataset_element_type` | `(table_name)` | Register table as a valid member source |
| `add_dataset_child` | `(parent_rid, child_rid)` | Nest dataset under parent |
| `list_dataset_children` | `(dataset_rid)` | List nested datasets |
| `list_dataset_parents` | `(dataset_rid)` | List parent datasets |
| `list_dataset_executions` | `(dataset_rid)` | List executions that consumed this dataset |
| `download_dataset` | `(dataset_rid, version, dest_dir, ...)` | Export as BDBag |
| `estimate_bag_size` | `(dataset_rid, version)` | Estimate download size |
| `validate_dataset_bag` | `(bag_path)` | Validate BDBag integrity and checksums |
| `denormalize_dataset` | `(dataset_rid, version, dest_dir, format)` | Export as flat CSV/Parquet |
| `create_dataset_type_term` | `(type_name, description)` | Add term to Dataset_Type vocabulary |
| `delete_dataset_type_term` | `(type_name)` | Remove term from Dataset_Type vocabulary |
| `restructure_assets` | `(dataset_rid, ...)` | Reorganize asset files in Hatrac storage |
| `split_dataset` | `(dataset_rid, train_pct, test_pct, ...)` | Split into train/test/validation subsets |

### 3.2 Execution Tools (all absent)

These 18 tools implement the DerivaML Execution abstraction: provenance tracking for ML
workflow runs including input datasets, output assets, and timing. All require `deriva-ml`
and belong in a `deriva-ml` plugin.

| Tool | Prototype signature | Description |
|---|---|---|
| `create_execution` | `(workflow_type, datasets, assets, description)` | Create execution with inputs |
| `start_execution` | `()` | Begin timing |
| `stop_execution` | `()` | End timing, mark complete |
| `update_execution_status` | `(status, message)` | Progress update |
| `set_execution_description` | `(execution_rid, description)` | Update description |
| `get_execution_info` | `()` | Get current execution state |
| `restore_execution` | `(execution_rid)` | Reconnect to an existing execution |
| `asset_file_path` | `(asset_table, filename, asset_types)` | Register output file for upload |
| `upload_execution_outputs` | `(clean_folder)` | Upload all staged files to catalog |
| `download_asset` | `(asset_rid, dest_dir)` | Download asset file from catalog |
| `create_execution_dataset` | `(description, dataset_types, version)` | Create output dataset within execution |
| `download_execution_dataset` | `(dataset_rid, version, dest_dir)` | Download BDBag from execution |
| `get_execution_working_dir` | `()` | Get temp working directory path |
| `add_nested_execution` | `(parent_rid, child_rid)` | Link child to parent execution |
| `list_nested_executions` | `(execution_rid)` | List child executions |
| `list_parent_executions` | `(execution_rid)` | List parent executions |
| `list_storage_contents` | `(hostname, catalog_id)` | List locally cached datasets/executions |
| `delete_storage` | `(rids, dry_run)` | Delete local cached data from `~/.deriva-ml/` |

### 3.3 Feature Tools (all absent)

These 5 tools implement the DerivaML Feature abstraction: provenance-tracked metadata
associations between domain records and controlled values or assets. All require
`deriva-ml` and belong in a `deriva-ml` plugin.

| Tool | Prototype signature | Description |
|---|---|---|
| `create_feature` | `(table_name, feature_name, comment, terms, assets, metadata)` | Create feature definition (association table) |
| `delete_feature` | `(table_name, feature_name)` | Delete feature definition |
| `add_feature_value` | `(table_name, feature_name, record_rid, term_value, ...)` | Add feature value to record |
| `add_feature_value_record` | `(table_name, feature_name, feature_record)` | Add value using Pydantic record |
| `fetch_table_features` | `(table_name)` | Get all features defined for a table |

### 3.4 Workflow Tools (all absent)

These 4 tools implement the DerivaML Workflow abstraction: categorization of execution
types. All require `deriva-ml` and belong in a `deriva-ml` plugin.

| Tool | Prototype signature | Description |
|---|---|---|
| `lookup_workflow_by_url` | `(url)` | Find workflow by URL |
| `create_workflow` | `(name, workflow_type, url, description)` | Create workflow definition |
| `set_workflow_description` | `(workflow_rid, description)` | Update workflow description |
| `add_workflow_type` | `(type_name, description)` | Add term to Workflow_Type vocabulary |

### 3.5 Background Task Tools (all absent)

These 4 tools manage long-running operations that are dispatched to a thread pool and
tracked with persistent state. Core has no background task infrastructure at all; long
operations run synchronously within the tool call.

| Tool | Prototype signature | Description |
|---|---|---|
| `clone_catalog_async` | `(source_hostname, source_catalog_id, root_rid, ...)` | Clone catalog as background task |
| `get_task_status` | `(task_id)` | Get task progress, result, and error |
| `list_tasks` | `(status_filter, task_type_filter)` | List all tasks for current user |
| `cancel_task` | `(task_id)` | Cancel a running task |

The prototype's `BackgroundTaskManager` also persists task state to disk
(`~/.deriva-ml/task_state.json`) and recovers crashed tasks on restart. Core has no
equivalent -- the expectation in core is that long-running RAG operations are deferred
or run at startup via asyncio tasks rather than surfaced to the LLM as background tasks.

### 3.6 Developer Tools (all absent)

These 6 tools assist with local development workflows and are not suitable for a
production multi-user deployment. They are prototype-only and out of scope for core.

| Tool | Prototype signature | Description |
|---|---|---|
| `bump_version` | `(bump_type, project_path)` | Bump git-tag semver version and push |
| `list_apps` | `()` | List available DerivaML web applications |
| `start_app` | `(app_id, hostname, catalog_id, app_path, port)` | Launch web app via reverse proxy |
| `stop_app` | `()` | Stop running web app |
| `inspect_notebook` | `(notebook_path)` | Inspect Jupyter notebook parameters |
| `run_notebook` | `(notebook_path, hostname, catalog_id, parameters, kernel)` | Execute notebook with papermill |

The prototype also ships a full reverse proxy server (`proxy.py`, ~635 lines) that
forwards `/ermrest`, `/authn`, and `/chaise` to a backend DERIVA server and serves
static web app files. It also exposes `/api/storage` endpoints for browsing and
deleting locally cached datasets. This entire subsystem has no equivalent in core.

### 3.7 Catalog Lifecycle Tools

Tools for creating, deleting, cloning, and aliasing catalogs. These are absent from core
because they operate at the server level and involve `deriva-ml` schema initialization.
Some (alias management) could reasonably be added to core as pure `DerivaServer` calls.

| Tool | Prototype signature | Description | Core feasibility |
|---|---|---|---|
| `create_catalog` | `(hostname, project_name, catalog_alias)` | Create catalog with ML schema | Needs `deriva-ml`; plugin |
| `delete_catalog` | `(hostname, catalog_id)` | Permanently delete catalog | Pure `DerivaServer`; could add to core |
| `apply_catalog_annotations` | `(navbar_brand_text, head_title)` | Chaise nav-bar annotations | Needs ML schema knowledge; plugin |
| `create_catalog_alias` | `(hostname, alias_name, catalog_id, name, description)` | Create URL alias | Pure `DerivaServer`; could add to core |
| `update_catalog_alias` | `(hostname, alias_name, alias_target, owner)` | Update alias | Pure `DerivaServer`; could add to core |
| `delete_catalog_alias` | `(hostname, alias_name)` | Delete alias | Pure `DerivaServer`; could add to core |
| `clone_catalog` | `(source_hostname, source_catalog_id, root_rid, ...)` | Workspace clone | Needs `deriva-ml`; plugin |
| `validate_rids` | `(dataset_rids, asset_rids, dataset_versions, ...)` | Batch RID validation | Needs `deriva-ml`; plugin |
| `cite` | `(rid, current)` | Citation URL | Pure catalog logic; could add to core |

### 3.8 Vocabulary Gaps

Tools present in the prototype that have no core counterpart:

| Tool | Prototype signature | Description |
|---|---|---|
| `create_vocabulary` | `(vocabulary_name, comment, schema)` | Create new vocabulary table with standard columns |

Tools that are consolidated in core (`update_term`) but explicit in the prototype:

| Prototype tool | Core approach |
|---|---|
| `add_synonym(vocabulary_name, term_name, synonym)` | Pass updated synonyms list to `update_term` |
| `remove_synonym(vocabulary_name, term_name, synonym)` | Pass updated synonyms list to `update_term` |
| `update_term_description(vocabulary_name, term_name, description)` | Pass updated description to `update_term` |

The consolidation in core is functionally equivalent but requires the caller to know the
existing synonyms before adding or removing one. The prototype's fine-grained tools are
more LLM-friendly because the LLM does not need to read-before-write.

### 3.9 Annotation Gaps

Tools in the prototype's annotation module with no core equivalent:

| Tool | Prototype signature | Description |
|---|---|---|
| `apply_annotations` | `()` | Commit all staged annotation changes to catalog |
| `reorder_visible_columns` | `(table_name, context, new_order)` | Reorder columns within visible-columns for a context |
| `reorder_visible_foreign_keys` | `(table_name, context, new_order)` | Reorder FKs within visible-foreign-keys for a context |
| `get_table_sample_data` | `(table_name, limit)` | Fetch sample rows to test Handlebars templates |
| `preview_handlebars_template` | `(template, data)` | Render Handlebars template with sample data |
| `validate_template_syntax` | `(template)` | Validate Handlebars template syntax |

`apply_annotations` has no equivalent because core does not use the staged model.

The three template-related tools (`get_table_sample_data`, `preview_handlebars_template`,
`validate_template_syntax`) are a useful ergonomic cluster for annotation authoring. They
have no DERIVA API dependency -- `preview_handlebars_template` uses a Python Handlebars
library, and `get_table_sample_data` is a simple `get_entities` call. These could be
added to core's annotation module.

`reorder_visible_columns` and `reorder_visible_foreign_keys` provide positional reordering
without requiring the caller to reconstruct the full annotation list. Both are achievable
in core with `set_visible_columns` / `set_visible_foreign_keys` but require a
read-before-write; the dedicated tools are more LLM-friendly.

### 3.10 Data Access Gaps

Convenience features in the prototype that are absent from core:

| Feature | Prototype | Core |
|---|---|---|
| Column projection | `query_table(columns=[...])` selects specific columns | `get_entities` returns all columns |
| Offset pagination | `query_table(offset=N)` | Not supported (fetches from start, cannot skip) |
| Count shorthand | `count_table(table_name, filters)` | No equivalent; use `query_aggregate` |
| Single-record fetch | `get_record(table_name, rid)` | No dedicated shorthand; use `get_entities` with filter |

All of these are achievable in core via `get_entities` plus client-side slicing, or via
`query_aggregate`, but with less convenience for the LLM. Adding `count_table` as a
shorthand to `query.py` is low-effort and high-value for LLM usability.

### 3.11 RAG Tool Gaps

| Feature | Prototype | Core |
|---|---|---|
| Full re-crawl | `rag_ingest(source_name)` -- blocking or background task | No equivalent; startup auto-update is incremental only |
| Runtime source add | `rag_add_source(name, repo_owner, ...)` | Only at server startup via plugin `ctx.rag_source()` |
| Runtime source remove | `rag_remove_source(name)` | No equivalent |

`rag_ingest` (force full re-crawl) is useful when the SHA-delta approach misses a change
(e.g., a file was edited but the tree SHA was not updated, or a full reindex is needed
after corruption). Core's `rag_update_docs` does the incremental update but there is no
"nuke and rebuild" escape hatch accessible to the LLM.

`rag_add_source` enables operators to add a new documentation repository without
redeploying. Core requires a plugin or code change. This is a deployment ergonomics gap
rather than a functional one.

### 3.12 Resources and Prompts (all absent)

The prototype registers a rich set of MCP resources and prompts that provide the LLM with
read-only context about the server and catalog state without consuming tool calls. Core
registers no resources or prompts.

**Prototype MCP resources (`resources.py`):**

| URI pattern | Type | Description |
|---|---|---|
| `deriva://config/hydra-zen` | Static | Hydra-zen config template |
| `deriva://config/execution` | Static | Execution configuration template |
| `deriva://catalog/info` | Dynamic | Active catalog hostname, ID, project name, schemas |
| `deriva://catalog/schema` | Dynamic | Full schema JSON (tables, columns, FKs) |
| `deriva://catalog/vocabularies` | Dynamic | All vocabulary terms for all vocabularies |
| `deriva://catalog/datasets` | Dynamic | Current dataset list with types and versions |
| `deriva://catalog/workflows` | Dynamic | Current workflow definitions |
| `deriva://dataset/{rid}` | Template | Details for a specific dataset |
| `deriva://vocabulary/{name}` | Template | Terms in a specific vocabulary |
| `deriva://docs/deriva-ml` | Dynamic | Fetched from GitHub (deriva-ml docs) |
| `deriva://docs/deriva-py` | Dynamic | Fetched from GitHub (deriva-py docs) |
| `deriva://docs/ermrest` | Dynamic | Fetched from GitHub (ermrest docs) |
| `deriva://docs/chaise` | Dynamic | Fetched from GitHub (chaise docs) |

**Prototype MCP prompts (`prompts.py`):**

The prototype ships extensive MCP prompts covering connection setup, dataset workflows,
execution patterns, feature authoring, schema design, and annotation guidance. These
provide the LLM with structured context for common workflows and reduce the number of
tool calls needed to orient a session. Core has no prompts.

The absence of resources and prompts is a significant usability gap. LLMs connecting to
core have no passive context and must discover capabilities entirely through tool
descriptions. For a multi-user HTTP deployment this matters less (each session is
purpose-driven), but for interactive development workflows the prototype's resources
substantially reduce friction.

---

## 4. Addendum -- In Core, Absent from Prototype

These capabilities exist in `deriva-mcp-core` and have no equivalent in the prototype.

### Per-request OAuth authentication

Core implements a full OAuth token pipeline:

- `CredenzaTokenVerifier` -- validates bearer tokens via Credenza introspection and
  exchanges them for DERIVA-scoped derived tokens on every HTTP request
- `DerivedTokenCache` -- smart cache keyed by `iss/sub` composite (not bare `sub`) to
  prevent cross-issuer collisions; near-expiry proactive eviction; per-principal async
  lock prevents duplicate exchanges under concurrent requests
- `IntrospectionCache` -- caches introspection results to avoid repeated round-trips to
  Credenza for the same token within its validity window
- `stateless_http=True` on `FastMCP` -- required for correct per-request contextvar
  propagation; stateful mode caused stale tokens to be reused after expiry (discovered
  and fixed in Phase 5.5)

The prototype uses only local disk credentials (`~/.deriva/credential.json`) with no
support for multi-user HTTP deployments.

### Mutation kill switch

Every tool must declare `mutates=True` or `mutates=False` at registration time (omitting
it raises `TypeError` at startup). `DERIVA_MCP_DISABLE_MUTATING_TOOLS=true` (the
default) blocks all `mutates=True` tools before they execute. This provides a safe
default for read-only deployments.

The prototype has no such safeguard; any tool can mutate the catalog by default.

### Structured JSON audit logging

Core emits structured JSON audit events for all auth lifecycle events (token introspection,
exchange, verification failures) and all catalog mutations (entity insert/update/delete,
Hatrac namespace creation), with `_failed` variants for each.

Events auto-inject `principal` (the `iss/sub` identity) from the per-request contextvar.
The audit log is separate from the application log and can write to syslog or a rotating
file.

The prototype tracks provenance implicitly through DerivaML execution records, but has
no standalone audit log.

### Plugin framework

The `PluginContext` API and entry-point loader allow external packages to extend the
server without modifying core code:

- `ctx.tool(mutates=)` -- register tools with mutation semantics
- `ctx.resource()` / `ctx.prompt()` -- register resources and prompts
- `ctx.on_catalog_connect(callback)` -- lifecycle hook; fires after any catalog access
- `ctx.on_schema_change(callback)` -- lifecycle hook; fires after schema mutations
- `ctx.rag_source(name, repo_owner, ...)` -- declare a documentation source for RAG
- Entry point group: `deriva_mcp.plugins`

The prototype has no plugin system.

### `deriva_call()` context manager

Wraps every DERIVA HTTP call. On a 401 response, evicts the stale derived token from the
cache (keyed by the current request's `iss/sub` principal) and re-raises. This handles
reactive token expiry: the next request from the LLM forces a fresh exchange transparently.

The implementation handles both `requests`-style exceptions (from `deriva-py` datapath
API, which wraps errors in `DataPathException` with a `caused_by` chain) and `httpx`-style
exceptions.

### Hostname remapping

`settings.hostname_map` allows Docker-internal hostnames to be rewritten to container
names before DERIVA API calls. For example, `localhost` in a tool call can be remapped to
`deriva-apache` for intra-container routing. The prototype implemented this as a
module-level `_resolve_hostname()` function only in `background_tasks.py`; core exposes
it uniformly for every tool.

### Hatrac tools

Three MCP tools for Hatrac object store operations, absent from the prototype:

- `list_namespace(hostname, path)` -- list objects and sub-namespaces
- `get_object_metadata(hostname, path)` -- fetch headers without downloading content
- `create_namespace(hostname, path)` -- create a namespace

### Schema introspection tools

Core provides explicit read tools for schema introspection:

- `get_catalog_info(hostname, catalog_id)` -- catalog-level metadata
- `list_schemas(hostname, catalog_id)` -- list schema names
- `get_schema(hostname, catalog_id, schema)` -- full schema with column and FK summaries
- `get_table(hostname, catalog_id, schema, table)` -- single table definition

The prototype had no equivalent tools; schema information was accessible only through
MCP resources or implicitly through the `DerivaML` model object.

### Annotation read tools

- `get_table_annotations(hostname, catalog_id, schema, table)` -- all annotations on a table
- `get_column_annotations(hostname, catalog_id, schema, table, column)` -- all annotations on a column
- `list_foreign_keys(hostname, catalog_id, schema, table)` -- FKs with their annotations

The prototype had no read tools for annotations; the LLM had to infer annotation content
from the schema resource.

### ERMrest path query tools

- `query_attribute(hostname, catalog_id, path, attributes)` -- attribute query with
  arbitrary caller-supplied ERMrest path expression; supports any traversal, join, or
  filter the ERMrest URL grammar allows
- `query_aggregate(hostname, catalog_id, path, aggregates)` -- aggregate query (count,
  sum, min, max, etc.) against an arbitrary path expression

The prototype's `query_table` only supported flat equality filters on a single table.

### RAG `rag_index_table` tool

Fetches all rows from a table and indexes them for semantic search, scoped to the calling
user's identity. Enables per-user data search when the LLM explicitly wants to query by
semantic similarity rather than by attribute value.

### pgvector backend for RAG

The prototype used only embedded ChromaDB. Core adds a `PgVectorStore` backed by
PostgreSQL with the `pgvector` extension, recommended for multi-instance deployments
where a shared index is needed.

### RAG per-user data indexing infrastructure

`index_table_data(store, hostname, catalog_id, table_name, rows, user_id, serializer?)`
is a public primitive callable by plugins. It serializes rows to Markdown with staleness
detection, generic column rendering, and a `RowSerializer` protocol for custom rendering.

The prototype had data indexing but it was not exposed as a pluggable primitive.

### Health endpoint

`/health` route on the HTTP server returns `{"status": "ok"}` without requiring
authentication. Suitable for Docker health probes and load balancer checks. The prototype
had no health endpoint.

### `ssl_verify` configuration

`DERIVA_MCP_SSL_VERIFY` allows disabling TLS certificate verification or specifying a
custom CA bundle path. The prototype disabled TLS verification unconditionally in
`proxy.py` (a known issue noted in CLAUDE.md).

---

*End of gap analysis.*