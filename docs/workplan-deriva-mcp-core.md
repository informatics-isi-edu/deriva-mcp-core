# deriva-mcp-core Workplan

## Overview

`deriva-mcp-core` is a new, MCP server reference implementation for the DERIVA platform.

Design goals:

- Proper per-request OAuth authentication (bearer token validation via Credenza introspection
  and token exchange)
- Core DERIVA primitives (schema introspection, entity CRUD, attribute/aggregate queries,
  Hatrac object store) implemented as built-in tools
- Built-in RAG subsystem covering DERIVA documentation and catalog schema, enabling
  semantic search and schema-aware LLM context without external services
- A clean plugin framework allowing higher-level tool sets (e.g., deriva-ml) to extend the
  server without modifying core code
- No backward compatibility requirements with the deriva-mcp prototype

---

## Architecture

### Transport and Auth Tiers

Two transport modes with distinct auth models:

| Mode              | Auth                                                        | Use Case                          |
|-------------------|-------------------------------------------------------------|-----------------------------------|
| `streamable-http` | Per-request bearer token (Credenza introspect + exchange)   | Production multi-user deployments |
| `stdio`           | Local credential from disk via deriva-py `get_credential()` | Local development only            |

The auth model is a property of the transport. No unification or awkward config gymnastics.

### Request Auth Flow (HTTP)

```
Bearer token in request
  -> CredenzaTokenVerifier.verify_token(): POST /introspect -> {sub, active, aud, ...}
  -> Validate aud contains server resource identifier
  -> DerivedTokenCache.get(sub, bearer_token)
       -> cache hit + not near-expiry: return cached derived token
       -> miss or near-expiry: POST /token (token_exchange) -> derived token
                               store (derived_token, expires_at) in cache
  -> Set _current_credential contextvar
  -> Tool / resource handler executes
       -> calls get_catalog(hostname, catalog_id)            [ERMREST tools]
            -> reads _current_credential from contextvar
            -> returns authenticated ErmrestCatalog directly
       -- or --
       -> calls get_hatrac_store(hostname)                   [Hatrac tools]
            -> reads _current_credential from contextvar
            -> returns authenticated HatracStore
       -- or --
       -> calls get_request_credential()               [higher-level API tools, e.g. deriva-ml]
            -> reads _current_credential from contextvar
            -> returns credential dict for passing to DerivaML(...) or similar
```

### Request Auth Flow (stdio)

```
Server startup
  -> deriva-py get_credential() reads ~/.deriva/credential.json
  -> Set as _current_credential (single-user, no exchange needed)
```

### Smart Token Cache

- Keyed by `principal` (`iss/sub` composite from introspection response) to prevent
  cross-issuer collisions -- Keycloak and Globus may both issue `sub=alice`, so bare
  `sub` is an insufficient cache key when multiple IDPs are configured
- Each entry stores `(derived_token, expires_at)`
- Near-expiry threshold: configurable, default 60 seconds before expiry (proactive eviction)
- Reactive eviction: `deriva_call()` context manager catches HTTP 401 responses from
  ERMrest or Hatrac, calls `invalidate_current_derived_token()` (keyed by the
  per-request `_current_user_id` contextvar which already holds `iss/sub`), then
  re-raises so the tool returns an error and the next request forces a fresh exchange
- Per-`principal` async lock prevents duplicate exchanges under concurrent requests
  (double-checked locking pattern)
- Cache is a server-level singleton

### Plugin Framework

External plugins declare a `register(ctx: PluginContext)` function and advertise it via
Python entry points:

```toml
# in plugin's pyproject.toml
[project.entry-points."deriva_mcp.plugins"]
my_plugin = "my_package.plugin:register"
```

Built-in core tool modules use the same `register(ctx: PluginContext)` signature, loaded
directly (not via entry points). This proves the plugin API with real usage without
artificial indirection.

`PluginContext` exposes:

- `ctx.tool(mutates=False)` -- decorator to register a read-only MCP tool; `mutates=` is
  required (omitting it raises `TypeError` at startup); pass `mutates=True` for tools that
  write to the DERIVA catalog or Hatrac object store; those tools are subject to the
  `DERIVA_MCP_DISABLE_MUTATING_TOOLS` kill switch
- `ctx.resource(uri_pattern)` -- decorator to register an MCP resource
- `ctx.prompt(name)` -- decorator to register an MCP prompt
- `ctx.on_catalog_connect(callback)` -- register a lifecycle hook called after any tool
  connects to a catalog (provides `hostname`, `catalog_id`, `schema_hash`, `schema_json`)
- `ctx.on_schema_change(callback)` -- register a lifecycle hook called after any mutating
  tool modifies a catalog's schema (provides `hostname`, `catalog_id`)
- `ctx.rag_source(name, repo_owner, repo_name, branch, path_prefix, doc_type?)` --
  declare a documentation source that the RAG subsystem should index; called from
  `register(ctx)` so sources are discovered automatically when the plugin is installed;
  is a no-op when `DERIVA_MCP_RAG_ENABLED=false` so plugins need no RAG guard logic

Lifecycle hooks are the mechanism by which the built-in RAG module (and external plugins)
can react to catalog activity without tight coupling to individual tools. The built-in RAG
module uses `on_catalog_connect` to trigger schema indexing; a plugin can use
`on_schema_change` to invalidate any schema-dependent state.

No `on_data_change` hook is provided. Data mutations (entity insert/update/delete) are the
hot path and event-driven reindexing on every write would add unacceptable embed+upsert
overhead. Data index freshness is instead managed by TTL-based staleness (default 3600s):
`index_table_data()` checks staleness before doing any work, so repeated calls are cheap.
A plugin that writes data and wants immediate reindex calls `index_table_data()` directly
after its own mutation -- no hook required, since the plugin already knows the write happened.

Inside any registered tool or resource, handlers access DERIVA via one of three public
functions imported from `deriva_mcp_core`:

- `get_catalog(hostname, catalog_id)` -- returns an authenticated `ErmrestCatalog` directly.
  Replaces the old two-step `get_deriva_server(hostname).connect_ermrest(catalog_id)`.
  Also fires the `_on_catalog_access` callback that triggers background schema indexing.
- `get_hatrac_store(hostname)` -- returns an authenticated `HatracStore` for object store
- `get_request_credential()` -- returns the credential dict for higher-level APIs that
  construct their own client:
  `DerivaML(hostname, catalog_id, credential=get_request_credential())`

Plugins never handle bearer token validation, introspection, or exchange. The credential
dict passed through `get_request_credential()` contains an opaque Credenza-derived token,
but the auth lifecycle is entirely managed by the core.

### RAG Subsystem

The RAG (Retrieval-Augmented Generation) subsystem indexes DERIVA documentation and catalog
schemas into a vector store, then exposes search as an MCP tool. This gives LLM clients
semantic awareness of the DERIVA API surface and of any specific catalog they are working
with -- without requiring the client to load entire schema documents into context on every
request.

#### What gets indexed

**Documentation index** (shared, all users):

Markdown documentation from four core DERIVA GitHub repositories:

| Source name      | Repository | Path prefix |
|------------------|------------|-------------|
| `deriva-py-docs` | deriva-py  | docs/       |
| `ermrest-docs`   | ermrest    | docs/       |
| `chaise-docs`    | chaise     | docs/       |

These repositories are public and contain the authoritative DERIVA API reference, user
guides, and integration documentation. The docs index is shared across all users and
updated at server startup (if stale) and on demand via `rag_update_docs`.

**Schema index** (per visibility class, all users):

When a tool connects to a catalog, the full schema is fetched via `/schema`, serialized to
structured Markdown, and indexed. A SHA-256 fingerprint of the schema JSON is used as a
visibility class key:

- Two users whose effective permissions produce identical `/schema` responses share one
  index entry (deduplicated automatically).
- A user with a restricted view gets their own index entry reflecting only what they can
  see.
- Source name: `schema:{hostname}:{catalog_id}:{schema_hash[:16]}`

The schema index gives the LLM knowledge of the specific catalog being queried: table names
and descriptions, column types and constraints, foreign key relationships, and vocabulary
term values. This is critical context for generating correct ERMrest query paths.

**Per-user data index** (generic primitives in core; schema-aware extension via plugin):

Core provides generic data indexing infrastructure keyed by user identity:

- Source name: `data:{hostname}:{catalog_id}:{user_id}`
- Generic row serialization: `RID` + `Description`/`Name` field + all non-system columns
  rendered to Markdown. Filters out DERIVA system columns (`RID`, `RCT`, `RMT`, `RCB`,
  `RMB`) automatically.
- Staleness detection: per-source `indexed_at` timestamp with configurable TTL (default
  3600 seconds). Skips re-indexing if fresh.
- `index_table_data(hostname, catalog_id, table_name, rows, user_id, serializer?)` --
  the core indexing primitive. Accepts an optional `RowSerializer` for custom rendering.
- `rag_index_table(hostname, catalog_id, schema, table)` MCP tool -- fetches rows via
  `get_entities()` and calls `index_table_data()` with the default generic serializer.
  Scoped to the calling user's identity.

Plugins extend data indexing by supplying a `RowSerializer` implementation:

```python
class RowSerializer(Protocol):
    def serialize(self, table_name: str, row: dict) -> str | None: ...
    # Return None to fall back to generic serialization for this row
```

A plugin calls `index_table_data()` directly (imported from `deriva_mcp_core.rag.data`)
using its own serializer and the `on_catalog_connect` or `on_schema_change` lifecycle
hooks as triggers. No special `PluginContext` registration is needed -- the primitives
are public functions.

The deriva-ml plugin will use this to index Dataset and Execution records with rich,
schema-aware Markdown (using DerivaML-typed fields, versioning, workflow relationships)
rather than the generic column dump.

#### Ingestion pipeline

```
Source (GitHub repo or catalog /schema)
  -> Crawl: fetch Markdown content (GitHub Trees API for docs, /schema JSON for catalog)
  -> Chunk: markdown-aware splitter at heading boundaries, ~800 tokens/chunk,
            1-sentence overlap, never splits inside code blocks
  -> Embed: local ONNX MiniLM-L6-v2 (384-dim, no external API, no network cost)
  -> Upsert: vector store with source/doc_type/heading metadata
```

Change detection for docs uses GitHub tree SHA comparison (no re-ingestion if tree
unchanged). Change detection for schema uses the visibility class hash (no re-ingestion if
schema unchanged for this user's visibility).

#### Vector store abstraction

The RAG module interacts with the vector store through a `VectorStore` protocol, not a
concrete ChromaDB or pgvector dependency:

```python
class VectorStore(Protocol):

    def upsert(self, chunks: list[Chunk]) -> None: ...

    def search(self, query: str, filters: dict, limit: int) -> list[SearchResult]: ...

    def delete_source(self, source_name: str) -> None: ...

    def has_source(self, source_name: str) -> bool: ...
```

Two implementations are shipped with core:

**`ChromaVectorStore`** (default for development):

- Embedded ChromaDB, zero additional services
- Persistent local storage, configurable via `DERIVA_MCP_RAG_CHROMA_DIR`
- Also supports ChromaDB server mode via `DERIVA_MCP_RAG_CHROMA_URL`
- Suitable for single-instance deployments and local development

**`PgVectorStore`** (recommended for production):

- PostgreSQL with the `pgvector` extension
- Configured via `DERIVA_MCP_RAG_PG_DSN`
- No new services required when DERIVA's existing PostgreSQL is reachable
- Scales correctly across multiple MCP server instances (shared index)
- Recommended for any deployment where more than one MCP server instance is running

The backend is selected via `DERIVA_MCP_RAG_VECTOR_BACKEND` (`chroma` or `pgvector`).
Plugin authors who need a custom backend implement the `VectorStore` protocol and pass
their instance to `PluginContext` (see plugin authoring guide).

#### MCP tools exposed

- `rag_search(query, limit?, source?, doc_type?)` -- semantic search across docs and
  schema indexes. The schema search is scoped automatically to the calling user's
  visibility class. Returns ranked results with text, relevance score, source, and
  heading context.
- `rag_update_docs(source_name?)` -- incremental update of one or all documentation
  sources (SHA delta, only re-indexes changed files). Safe to call frequently.
- `rag_index_schema(hostname, catalog_id)` -- manual trigger to index or refresh the
  schema for a specific catalog. Normally triggered automatically via `on_catalog_connect`
  lifecycle hook; this tool is provided for explicit control.
- `rag_index_table(hostname, catalog_id, schema, table)` -- fetch all rows from a table
  via `get_entities()` and index them using the generic row serializer. Scoped to the
  calling user. A plugin with a custom serializer calls `index_table_data()` directly
  rather than using this tool.
- `rag_status()` -- returns per-source chunk counts, last-indexed timestamps, schema
  visibility classes, and per-user data index staleness information.

#### Multi-user deployment notes

- The docs index is fully shared -- one copy for all users, no per-user duplication.
- Schema indexes are deduplicated by visibility class hash. In a deployment where all
  users share the same schema ACL, there is effectively one schema index per catalog.
- The RAG module itself holds no per-request state. All scoping is done at query time
  via metadata filters (source name, schema hash).
- For the `PgVectorStore` backend, the MCP server connects with a service account that
  has read/write access to the vector tables. This is separate from the per-request
  Credenza-derived credential used for DERIVA REST access.

### Configuration

`pydantic-settings` `BaseSettings` with `env_prefix="DERIVA_MCP_"`. Supports environment
variables and optional env file. The env file is located by searching these paths in order
(first found wins): `/etc/deriva-mcp/deriva-mcp.env`, `~/deriva-mcp.env`, `./deriva-mcp.env`.
Override with `deriva-mcp-core --config /path/to/file.env`. Environment variables always
take precedence over the env file.

**Auth (required for HTTP transport):**

| Variable                                | Required | Default                       | Description                                          |
|-----------------------------------------|----------|-------------------------------|------------------------------------------------------|
| `DERIVA_MCP_CREDENZA_URL`               | Yes      | --                            | Credenza base URL                                    |
| `DERIVA_MCP_SERVER_URL`                 | Yes      | --                            | Public HTTPS URL of this MCP server                  |
| `DERIVA_MCP_SERVER_RESOURCE`            | Yes      | --                            | Resource identifier for this MCP server (URI or URN) |
| `DERIVA_MCP_CLIENT_SECRET`              | Yes      | --                            | This server's client secret                          |
| `DERIVA_MCP_DERIVA_RESOURCE`            | No       | `urn:deriva:rest:service:all` | Resource identifier to exchange to (DERIVA REST)     |
| `DERIVA_MCP_CLIENT_ID`                  | No       | `deriva-mcp`                  | This server's client ID (confidential client)        |
| `DERIVA_MCP_TOKEN_CACHE_BUFFER_SECONDS` | No       | `60`                          | Near-expiry buffer for derived token cache           |

**Logging:**

| Variable            | Required | Default | Description                                         |
|---------------------|----------|---------|-----------------------------------------------------|
| `DERIVA_MCP_DEBUG`  | No       | `false` | Set to `true` to enable DEBUG-level logging         |

**Safety:**

| Variable                            | Required | Default | Description                                                                                                                                     |
|-------------------------------------|----------|---------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| `DERIVA_MCP_DISABLE_MUTATING_TOOLS` | No       | `true`  | Kill switch for all tools registered with `mutates=True`. Defaults to enabled -- operators must explicitly set `false` to allow catalog writes. |

**Audit logging:**

| Variable                        | Required | Default                | Description                                               |
|---------------------------------|----------|------------------------|-----------------------------------------------------------|
| `DERIVA_MCP_AUDIT_LOGFILE_PATH` | No       | `deriva-mcp-audit.log` | Path for rotating JSON audit log file                     |
| `DERIVA_MCP_AUDIT_USE_SYSLOG`   | No       | `false`                | Write audit events to syslog (`/dev/log`) instead of file |

**RAG:**

| Variable                          | Required | Default             | Description                                                |
|-----------------------------------|----------|---------------------|------------------------------------------------------------|
| `DERIVA_MCP_RAG_ENABLED`          | No       | `true`              | Enable or disable the RAG subsystem entirely               |
| `DERIVA_MCP_RAG_VECTOR_BACKEND`   | No       | `chroma`            | Vector store backend: `chroma` or `pgvector`               |
| `DERIVA_MCP_RAG_CHROMA_DIR`       | No       | `~/.deriva-mcp/rag` | Embedded ChromaDB persistence directory (`chroma` backend) |
| `DERIVA_MCP_RAG_CHROMA_URL`       | No       | --                  | ChromaDB server URL (overrides dir, enables server mode)   |
| `DERIVA_MCP_RAG_PG_DSN`           | No       | --                  | PostgreSQL DSN for pgvector backend                        |
| `DERIVA_MCP_RAG_AUTO_UPDATE`      | No       | `true`              | Re-crawl documentation sources at server startup           |
| `DERIVA_MCP_RAG_DATA_TTL_SECONDS` | No       | `3600`              | Data index staleness TTL; reindex if older than this       |

Resource identifiers may be HTTPS URIs or URNs; Credenza accepts both.

---

## Package Structure

```
deriva-mcp-core/
├── pyproject.toml
├── docs/
└── src/
    └── deriva_mcp_core/
        ├── __init__.py          # Public API: get_catalog(), get_hatrac_store(), get_request_credential()
        ├── server.py            # FastMCP server factory + transport dispatch
        ├── config.py            # Pydantic Settings (DERIVA_MCP_* env vars)
        ├── context.py           # Per-request contextvar (_current_credential)
        ├── auth/
        │   ├── __init__.py
        │   ├── introspect.py    # Credenza POST /introspect client
        │   ├── exchange.py      # Credenza POST /token token_exchange client
        │   ├── token_cache.py   # Smart derived token cache
        │   └── verifier.py      # CredenzaTokenVerifier (mcp TokenVerifier protocol)
        ├── plugin/
        │   ├── __init__.py
        │   ├── api.py           # PluginContext class (tool/resource/prompt + lifecycle hooks)
        │   └── loader.py        # Entry point discovery and registration
        ├── rag/                 # Built-in RAG subsystem
        │   ├── __init__.py      # RAGManager singleton; register(ctx) entry point
        │   ├── config.py        # RAGSettings (DERIVA_MCP_RAG_* vars)
        │   ├── store.py         # VectorStore protocol + ChromaVectorStore + PgVectorStore
        │   ├── chunker.py       # Markdown-aware document chunking
        │   ├── crawler.py       # GitHub repo crawler (Trees API, SHA change detection)
        │   ├── docs.py          # Documentation source ingestion pipeline
        │   ├── schema.py        # Catalog schema indexing (visibility class isolation)
        │   └── data.py          # Generic data indexing primitives + RowSerializer protocol
        ├── telemetry/           # Observability
        │   ├── __init__.py      # Re-exports audit_event, init_audit_logger
        │   └── audit/
        │       ├── __init__.py
        │       └── logger.py    # Structured JSON audit log (python-json-logger); syslog or rotating file
        └── tools/               # Built-in DERIVA tools (each has register(ctx))
            ├── __init__.py
            ├── catalog.py       # Schema introspection
            ├── entity.py        # Entity CRUD (get, post, put, delete)
            ├── query.py         # Attribute and aggregate queries
            └── hatrac.py        # Hatrac object store
```

---

## Phases

---

### Phase 0 -- Repo and Project Scaffolding [DONE]

**Goal:** Establish the repo, package structure, and development environment. No functional
code yet.

#### 0.1 Repo Initialization

- Create `deriva-mcp-core` repo
- Initialize `pyproject.toml` with `uv` / `hatchling`
- Set up `src/deriva_mcp_core/` package layout
- Configure `ruff` for lint and format
- Configure `pytest` with `pytest-asyncio`

#### 0.2 Dependencies

Core runtime dependencies:

- `mcp` (>=1.9.0, provides FastMCP and streamable-http transport)
- `pydantic-settings`
- `deriva-py` (for `ErmrestCatalog`, `HatracStore`, `get_credential`)
- `httpx` (async HTTP for Credenza introspect + exchange calls)

Dev dependencies:

- `pytest`, `pytest-asyncio`, `pytest-httpx` (mock Credenza endpoints)
- `ruff`

#### 0.3 CI Scaffold

- `Makefile` or `justfile` with `test`, `lint`, `format` targets

Deliverable: `uv sync` and `pytest` run cleanly (zero tests, no errors).

---

### Phase 1 -- Configuration and Context [DONE]

**Goal:** Config model and per-request credential contextvar in place. No auth calls yet.

#### 1.1 Config Model

- `config.py`: `Settings(BaseSettings)` with all `DERIVA_MCP_*` variables
- `.env` file support
- `validate_for_http()`: raises `ValueError` listing all missing required fields; called
  at HTTP transport startup, not needed for stdio

#### 1.2 Per-Request Context

- `context.py`: `_current_credential: ContextVar[dict | None]`
- `set_current_credential(cred: dict)` -- called by auth verifier or startup (internal)
- `get_request_credential() -> dict` -- public; raises if called outside handler context
- `get_catalog(hostname, catalog_id) -> ErmrestCatalog` -- public; reads credential, constructs and returns an
  authenticated ErmrestCatalog
- `get_hatrac_store(hostname) -> HatracStore` -- public; reads credential, constructs HatracStore

#### 1.3 Public API

Three functions exported from `deriva_mcp_core`:

- `get_catalog(hostname, catalog_id) -> ErmrestCatalog` -- authenticated ErmrestCatalog.
  Replaces the old two-step `get_deriva_server(hostname).connect_ermrest(catalog_id)`.
  Also fires the `_on_catalog_access` callback that triggers background schema indexing.

- `get_hatrac_store(hostname) -> HatracStore` -- authenticated connection root for Hatrac object
  store operations. Separate from DerivaServer (distinct URL base, /hatrac/).

- `get_request_credential() -> dict` -- returns the per-request credential dict. Use when
  passing to a higher-level API that constructs its own client, e.g.:
  `DerivaML(hostname, catalog_id, credential=get_request_credential())`. Named distinctly
  from `deriva.core.get_credential()` (which reads from local disk) to avoid confusion for
  developers familiar with deriva-py.

Deliverable: `get_catalog()`, `get_hatrac_store()`, and `get_request_credential()` work with a
manually set credential. Unit tested.

---

### Phase 2 -- Auth Layer [DONE]

**Goal:** Credenza introspection, token exchange, and smart cache implemented and tested.
No server wiring yet.

#### 2.1 Introspection Client

- `auth/introspect.py`: async `introspect(token: str) -> IntrospectionResult`
- `IntrospectionResult`: dataclass with `active`, `sub`, `aud`, `exp`, payload
- Uses `httpx.AsyncClient` with client credentials (`CLIENT_ID`, `CLIENT_SECRET`)
- Raises on inactive token or missing `sub`

#### 2.2 Token Exchange Client

- `auth/exchange.py`: async `exchange(subject_token: str, target_resource: str) -> ExchangeResult`
- `ExchangeResult`: dataclass with `access_token`, `expires_in`, computed `expires_at`
- RFC 8693 POST to Credenza `/token`

#### 2.3 Smart Token Cache

- `auth/token_cache.py`: `DerivedTokenCache`
- `async get(principal: str, subject_token: str) -> str`
    - Cache hit + not near expiry: return immediately
    - Miss or near expiry: acquire per-`principal` asyncio.Lock, recheck, exchange, store,
      emit `token_exchange_success` audit event, return
- `invalidate(principal: str)` -- for explicit eviction (e.g., on 401 from downstream)
- Cache entries: `{principal: CacheEntry(derived_token, expires_at)}`
- `principal` is `iss/sub` composite (not bare `sub`) to prevent cross-issuer collisions

Deliverable: Auth layer fully unit tested with `pytest-httpx` mocks of Credenza endpoints.
No live Credenza dependency required for tests.

---

### Phase 3 -- Server Skeleton and Token Verifier [DONE]

**Goal:** A running FastMCP server with auth wiring and plugin framework in place.
No tools yet, but the full auth pipeline is end-to-end functional and the plugin
API (including lifecycle hooks) is finalized.

#### 3.1 Token Verifier

`mcp` 1.9+ provides a `TokenVerifier` protocol and a `token_verifier` parameter on
`FastMCP.__init__`. We implement this protocol rather than hand-rolling Starlette middleware.

- `auth/verifier.py`: `CredenzaTokenVerifier` implementing `mcp.server.auth.provider.TokenVerifier`
    - `async verify_token(token: str) -> AccessToken | None`
    - Calls `introspect(token)` -> validates `active` and `aud` contains `DERIVA_MCP_SERVER_RESOURCE`
    - Calls `token_cache.get(principal, token)` -> derived token
    - Calls `set_current_credential({"bearer-token": derived_token})`
    - Returns `AccessToken(token=derived_token, client_id=principal, scopes=..., expires_at=..., resource=...)`
    - Returns `None` on introspection failure, inactive token, or `aud` mismatch (FastMCP
      returns 401 automatically)

`middleware.py` is not needed; `CredenzaTokenVerifier` integrates directly with FastMCP's
built-in auth layer.

#### 3.2 Server Factory

- `server.py`: `create_server() -> FastMCP`
    - HTTP mode: `FastMCP("deriva", token_verifier=CredenzaTokenVerifier(...))` with
      `streamable-http` transport
    - stdio mode: call deriva-py `get_credential()` at startup, set contextvar, configure
      FastMCP with `stdio` (no token verifier needed)
- CLI entrypoint: `deriva-mcp-core` command with `--transport`, `--host`, `--port` args
- `/health` custom route (no auth, returns `{"status": "ok"}`)

#### 3.3 Plugin Loader

- `plugin/api.py`: `PluginContext` -- wraps `FastMCP` instance, exposes:
    - `tool()`, `resource(uri_pattern)`, `prompt(name)` decorators
    - `on_catalog_connect(callback)` -- lifecycle hook; `callback(hostname, catalog_id, schema_hash)`
    - `on_schema_change(callback)` -- lifecycle hook; `callback(hostname, catalog_id)`
- `plugin/loader.py`: `load_plugins(ctx: PluginContext)` -- discovers `deriva_mcp.plugins`
  entry points, calls each `register(ctx)`

#### 3.4 Lifecycle Hook Dispatch

Tools that connect to a catalog or mutate schema are responsible for firing the appropriate
hooks via helpers in `plugin/api.py`:

- `fire_catalog_connect(hostname, catalog_id, schema_hash, schema_json)` -- called by schema
  introspection tools after fetching `/schema`; passes the already-fetched schema JSON
  directly so hook callbacks never need to re-fetch it
- `fire_schema_change(hostname, catalog_id)` -- called by tools that modify the schema
  (table create/delete, column changes, etc.)

Hooks are dispatched asynchronously (fire-and-forget) so they do not block the tool response.
Exceptions in hook callbacks are logged and suppressed -- a failing RAG reindex must never
cause a tool call to fail.

Deliverable: Server starts, unauthenticated requests are rejected by FastMCP's auth layer,
`/health` responds without auth. Plugin lifecycle hook API is implemented and tested.
Integration test with mocked Credenza confirms auth pipeline end-to-end.

---

### Phase 4 -- Core ERMREST Tools and RAG Subsystem [PARTIALLY DONE]

**Goal:** Built-in tools covering the ERMREST primitive surface, plus the RAG subsystem
for documentation and schema search.

Each tool module has a `register(ctx: PluginContext)` function (same pattern as external
plugins). Registered directly in `server.py` before plugin discovery.

#### 4.1 Schema Introspection (`tools/catalog.py`)

- `get_catalog_info(hostname, catalog_id)` -- catalog metadata
- `list_schemas(hostname, catalog_id)` -- schema names
- `get_schema(hostname, catalog_id, schema)` -- tables, columns, keys, FKs for a schema
- `get_table(hostname, catalog_id, schema, table)` -- full table definition

Schema introspection tools fire `on_catalog_connect` after fetching the schema, passing the
computed `schema_hash`. This is the trigger for RAG schema indexing.

#### 4.2 Entity CRUD (`tools/entity.py`)

- `get_entities(hostname, catalog_id, schema, table, filters?)` -- entity retrieval
- `insert_entities(hostname, catalog_id, schema, table, entities)` -- POST
- `update_entities(hostname, catalog_id, schema, table, entities)` -- sparse PUT
- `delete_entities(hostname, catalog_id, schema, table, filters)` -- DELETE

All four tools use `catalog.getPathBuilder()` (deriva-py datapath API) rather than raw
ERMrest URLs. In particular:

- `update_entities` uses `EntitySet.update()` which sends `PUT /attributegroup` with only
  the columns present in the entity dict as targets. Columns omitted from the input are
  never nulled out -- this is the correct behavior for sparse patch semantics. (Raw
  `PUT /entity` would null omitted nullable columns, which caused data loss in live testing.)
- `get_entities` uses `path.entities().fetch(limit=min(limit, 1000))`
- `delete_entities` requires non-empty `filters` to prevent accidental full-table deletion

All tools are wrapped with `with deriva_call():` for reactive 401 eviction (see 4.8).

On not-found errors (ERMrest table/schema 404 patterns), tools call `_rag_suggestions()`
to include "did you mean?" hints in the error response when the RAG subsystem is enabled.

#### 4.3 Queries (`tools/query.py`)

- `query_attribute(hostname, catalog_id, path, attributes?)` -- attribute query
- `query_aggregate(hostname, catalog_id, path, aggregates)` -- aggregate query

The `path` parameter is a caller-supplied ERMrest path expression (e.g.,
`"isa:Dataset/Status=released"`). Raw HTTP (`catalog.get(url)`) is appropriate here
because the full path expression is inherently a URL fragment -- there is no datapath
equivalent for arbitrary user-supplied traversal expressions. Both tools are wrapped
with `with deriva_call():`.

#### 4.4 Hatrac Object Store (`tools/hatrac.py`)

- `list_namespace(hostname, path)` -- list objects in namespace
- `get_object_metadata(hostname, path)` -- object metadata (not content)
- `create_namespace(hostname, path)` -- create namespace

No datapath equivalent exists for Hatrac -- raw `HatracStore` HTTP calls are appropriate.
All three tools are wrapped with `with deriva_call():`.

#### 4.5 RAG Subsystem (`rag/`)

The RAG module is registered as a built-in plugin via `register(ctx)` in `rag/__init__.py`.
It wires up the `on_catalog_connect` lifecycle hook and exposes four MCP tools.

**`rag/config.py`** -- `RAGSettings(BaseSettings)` covering all `DERIVA_MCP_RAG_*` variables.
Validates that the selected backend has its required config (e.g., `PG_DSN` required if
`VECTOR_BACKEND=pgvector`).

**`rag/store.py`** -- `VectorStore` protocol and two concrete implementations:

- `ChromaVectorStore`: embedded ChromaDB (default); optional `chroma_url` for server mode
- `PgVectorStore`: pgvector via asyncpg; recommended for production multi-instance deployments

Both implementations support `upsert`, `search` (with metadata filter support), `delete_source`,
and `has_source`. The schema for the pgvector table is managed by `PgVectorStore` itself
(creates table and index if absent).

**`rag/chunker.py`** -- Markdown-aware splitter:

- Splits at `##` and `###` heading boundaries only
- Never splits inside fenced code blocks
- Target chunk size: ~800 tokens (approximated by word count)
- One-sentence overlap at chunk boundaries for context continuity
- Returns `Chunk` dataclasses with `text`, `source`, `doc_type`, `heading_hierarchy`,
  `section_heading` metadata

**`rag/crawler.py`** -- GitHub documentation crawler:

- Uses GitHub Trees API (`GET /repos/{owner}/{repo}/git/trees/{branch}?recursive=1`)
  to discover all `.md` files under a path prefix
- Fetches file content from GitHub raw URLs
- Stores per-file SHA for incremental updates (only re-fetches changed files)
- No GitHub authentication required for public repositories

**`rag/docs.py`** -- Documentation ingestion:

- `ingest_docs(source_name?, force?)` -- full crawl of one or all documentation sources
- `update_docs(source_name?)` -- incremental update using SHA delta (preferred)
- Default sources: `deriva-py-docs`, `ermrest-docs`, `chaise-docs`

Documentation sources are registered through two paths, merged at startup:

1. **Plugin-declared** (via `ctx.rag_source()` in `register(ctx)`): sources declared by
   installed plugins. Discovered automatically -- no admin action required after plugin
   installation. The deriva-ml plugin uses this to register its own docs.

2. **Runtime-added** (via `rag_add_source` tool, persisted to `~/.deriva-mcp/rag/sources.json`):
   operator-managed sources added after deployment. Survives restarts via the JSON file.

At startup, the RAG manager merges both lists, deduplicates by name (plugin-declared takes
precedence on conflict), and ingests any source not yet indexed or whose tree SHA has changed.

**`rag/schema.py`** -- Catalog schema indexing:

- `index_schema(hostname, catalog_id, schema_json)` -- serialize schema to Markdown,
  chunk, and upsert into vector store under the visibility class source name
- Schema serialization covers: schema names and comments, table names/types/comments,
  columns (name, type, nullability, comment), foreign key relationships, and vocabulary
  terms (name, synonyms, URI) for tables flagged as vocabulary
- `schema_hash(schema_json) -> str` -- SHA-256 fingerprint of schema JSON for visibility
  class key; truncated to 16 hex characters in source name
- `has_schema(hostname, catalog_id, schema_hash) -> bool` -- check before indexing to skip
  unchanged visibility classes

**`rag/data.py`** -- Generic per-user data indexing primitives:

- `RowSerializer` protocol -- `serialize(table_name, row) -> str | None`; return `None` to
  fall back to generic rendering for that row
- `data_source_name(hostname, catalog_id, user_id) -> str` -- source naming convention
- `index_table_data(hostname, catalog_id, table_name, rows, user_id, serializer?)` --
  public function; serialize rows to Markdown chunks and upsert into vector store; applies
  staleness check and clears stale entries before reindexing; callable by plugins directly
- Generic row serialization: `## {table_name}: {desc} (RID: {rid})` header followed by
  `**{column}:** {value}` lines for all non-system columns with non-null values
- Staleness detection: checks `indexed_at` metadata against configurable TTL; skips
  reindex if fresh

Plugins import `index_table_data` and `RowSerializer` from `deriva_mcp_core.rag.data`
and call `index_table_data()` with their own serializer from a lifecycle hook:

```python
# in a plugin's register(ctx):
from deriva_mcp_core.rag.data import index_table_data, RowSerializer


class MySerializer:
    def serialize(self, table_name: str, row: dict) -> str | None:
        if table_name == "Dataset":
            return _rich_dataset_markdown(row)
        return None  # fall back to generic for other tables


ctx.on_catalog_connect(_reindex_data)
```

**MCP tools registered by `rag/__init__.py`:**

- `rag_search(query, limit?, hostname?, catalog_id?, doc_type?)` -- semantic search.
  When `hostname` and `catalog_id` are provided, schema search is scoped to the calling
  user's current visibility class hash (read from a per-request cache populated by the
  `on_catalog_connect` hook). Doc search is always global.
- `rag_update_docs(source_name?)` -- incremental docs update (SHA delta).
- `rag_index_schema(hostname, catalog_id)` -- manual schema reindex trigger.
- `rag_status()` -- per-source chunk counts, timestamps, and known schema visibility classes.

**Lifecycle hook wiring:**

```python
# in rag/__init__.py register(ctx):
ctx.on_catalog_connect(_handle_catalog_connect)


async def _handle_catalog_connect(hostname, catalog_id, schema_hash, schema_json):
    if not rag_manager.has_schema(hostname, catalog_id, schema_hash):
        await rag_manager.index_schema(hostname, catalog_id, schema_json)
```

The schema JSON is passed directly through the hook. It is already in memory (the
introspection tool just fetched it) and passing it as a function argument to a
fire-and-forget async task has negligible cost. The async task holds a reference until
indexing completes, then it is GC'd. There is no service-level credential and no
re-fetch -- the hook has everything it needs from the original request.

#### 4.8 Reactive 401 Eviction (`context.deriva_call()`)

`deriva_call()` is a context manager exported from `deriva_mcp_core.context` (and
re-exported from the package root). It wraps any block of code that makes DERIVA or
Hatrac HTTP calls and handles stale derived-token eviction reactively:

```python
with deriva_call():
    catalog = get_catalog(hostname, catalog_id)
    result = catalog.getPathBuilder().schemas[s].tables[t].entities().fetch()
```

Implementation:

- `_is_401(exc)` -- duck-typed 401 detection: checks `getattr(exc, "response", None)`
  and `response.status_code == 401`; works with both `requests` and `httpx` exceptions
- On a 401: calls `invalidate_current_derived_token()`, which reads `_current_user_id`
  (already the `iss/sub` principal) and calls `_token_cache_ref.invalidate(principal)`;
  no-ops gracefully in stdio mode where no cache is registered
- Re-raises the original exception unconditionally -- the tool's existing error-return
  path handles it; the next request from the LLM will force a fresh token exchange

`_set_token_cache(cache)` is called from `server.py` at HTTP startup so the module-level
reference is available before any request arrives. In stdio mode it is never called and
the reference stays `None`.

Plugin tools written for any DERIVA server should wrap catalog calls with `deriva_call()`
to get the same reactive eviction behavior automatically.

#### 4.6 Audit Logging (`telemetry/audit/`)

Structured JSON audit log following the Credenza pattern (`python-json-logger`,
syslog or `TimedRotatingFileHandler` fallback, separate from the application log).

- `audit_event(event, **kwargs)` -- emits a JSON record with `event`, `timestamp`, and
  auto-injected `principal` (from per-request contextvar; omitted for pre-auth events
  where no identity is available yet)
- Auth events: `token_inactive`, `token_introspection_failed`,
  `token_audience_mismatch` (with principal), `token_exchange_failed` (with principal)
  -- all from `verifier.py`; `token_exchange_success` (with principal, expires_in)
  from `token_cache.py` on actual cache-miss exchanges only; `token_verified`
  (with display name) from `verifier.py`
- Mutation events in `entity.py` and `hatrac.py`: `entity_insert`, `entity_update`,
  `entity_delete` (with verbatim filters), `hatrac_create_namespace`, plus `_failed`
  variants for each with `error_type`
- `context.get_request_user_id_optional() -> str | None` -- nullable identity accessor
  used by the audit layer to distinguish "not yet authenticated" from "stdio transport"
- App-level logging (`_init_logging`) uses Credenza-compatible format and is called from
  `main()` only (not `create_server()`) to avoid interfering with pytest `caplog`

#### 4.7 Mutation Kill Switch

- `ctx.tool(mutates=)` requires an explicit `True`/`False` declaration on every tool
  registration; omitting it raises `TypeError` at server startup so undeclared tools
  are caught before production
- When `DERIVA_MCP_DISABLE_MUTATING_TOOLS=true` (default), tools registered with
  `mutates=True` return `{"error": "catalog mutations are disabled by server configuration"}`
  without executing
- RAG tools use `mutates=False` -- they write to the local vector store, not the DERIVA
  catalog, and should not be blocked by the kill switch
- Server logs a `WARNING` when mutations are disabled and `INFO` when enabled, so
  operators can confirm the active state at startup

Status notes:

- ERMREST tools (4.1-4.4), audit logging (4.6), mutation kill switch (4.7), and
  reactive 401 eviction (4.8) are implemented and unit tested.
- entity.py was rewritten to use the deriva-py datapath API (see 4.2); `update_entities`
  now uses `PUT /attributegroup` for sparse updates -- validated against a live catalog
  (2026-03-23); the previous raw `PUT /entity` implementation caused nullable columns to
  be nulled when not included in the payload.
- Token cache key changed from bare `sub` to `iss/sub` composite (`principal`) to prevent
  cross-issuer collisions in multi-IDP deployments (Keycloak + Globus share `sub` space).
- RAG subsystem (4.5) is implemented and unit tested but has NOT been validated
  end-to-end against real GitHub content, a live ChromaDB instance, or a real DERIVA
  catalog. The chunker, crawler, store, and schema/data indexing pipelines are tested
  with mocks only. Treat as unproven until Phase 5 integration testing confirms it.

Deliverable: All built-in tools implemented and unit tested. Structured audit log and
mutation kill switch in place. RAG subsystem pending live validation (see Phase 5).

---

### Phase 5 -- Integration and Validation [IN PROGRESS]

**Goal:** End-to-end validation against live Credenza + DERIVA, and RAG subsystem
validated against real documentation content and a real catalog.

#### 5.1 Integration Tests

- Test `/authorize` -> `/token` -> MCP bearer token flow against Credenza
- Test token verifier rejects expired/invalid tokens
- Test token cache hit/miss/near-expiry behavior under concurrent requests
- Test schema introspection tools against a real DERIVA catalog

#### 5.2 RAG Integration Tests [DONE]

Automated tests (in `tests/test_rag_integration.py`, marker `rag`):

- `ChromaVectorStore` CRUD and semantic search with embedded ChromaDB
- Schema indexing and visibility class deduplication against real ChromaDB
- `on_catalog_connect` lifecycle hook wires up and indexes schema correctly
- `PgVectorStore` CRUD, search, and schema indexing via `testing.postgresql`
  (skipped on Windows; requires `pgvector` extension installed on the host)

These run without live external services. Run with:
`uv run pytest -m rag`

Human-in-the-loop validation completed 2026-03-23 (Claude Desktop + Claude Code):

- Docs ingestion against real GitHub repositories (ermrest-docs, deriva-py)
- Full end-to-end `rag_search` against a real DERIVA catalog with live data
- Entity CRUD tools validated against a live local catalog (query and mutation)
- pgvector backend validation on Linux with pgvector installed: still TODO

#### 5.3 Plugin Smoke Test

- Write a minimal `test_plugin` (in the test suite, not a separate package) that uses
  `PluginContext` to register a custom tool and a lifecycle hook
- Verify the tool is reachable and executes correctly with proper auth context
- Verify the lifecycle hook fires when the built-in schema introspection tools run

#### 5.4 Docker Compose Configuration

- `docker-compose.yml` for running `deriva-mcp-core` alongside Credenza
- Include PostgreSQL with pgvector extension as the recommended RAG backend
- Document required env vars for both ChromaDB and pgvector configurations
- Confirm `/health` suitable for Docker health probes

Deliverable: Full integration test suite passing. Reference compose config documented.

---

### Phase 5.5 -- Tool Parity Extension [DONE -- 2026-03-24]

**Goal:** Close the tool coverage gap between `deriva-mcp-core` and the `deriva-mcp`
prototype by adding annotation, schema/DDL, and vocabulary tools.

**Completed:** All three tool modules implemented, registered, and unit tested.
Test count: 234 passing, 6 skipped (up from 181 before this phase).

Post-5.5 improvements also completed (2026-03-24):

- `get_catalog(hostname, catalog_id)` replaces `get_deriva_server(hostname).connect_ermrest(catalog_id)`
  across all tool modules, context.py, rag/__init__.py, and tests. `get_deriva_server` removed entirely.
- `DERIVA_MCP_DEBUG=true` env var wired through `Settings.debug` to `_init_logging()`.
- `token_exchange_success` audit event added to `token_cache.py` (cache-miss exchanges only).
- Token cache key renamed from `sub` to `principal` (`iss/sub` composite) throughout.
- Chroma `where` clause fix: `_to_chroma_where` now emits `{"field": {"$eq": value}}` syntax
  (bare `{"field": value}` is rejected by current Chroma versions).
- RAG entity error suggestions: pre-filter to `doc_type="schema"` so doc chunks cannot crowd
  out schema chunks; schema-name re-ranking so keyword-matched entries beat embedding rank;
  improved hint format ("Did you mean schema: 'Data'? (e.g. Data:Collection)") for table
  lookups where the schema name needs correcting.

#### 5.5.1 Datapath API Assessment Summary

| Module       | API Strategy                              | Status                                                                 |
|--------------|-------------------------------------------|------------------------------------------------------------------------|
| `entity.py`  | Datapath: `getPathBuilder()` + `path.*`   | Done                                                                   |
| `catalog.py` | Raw HTTP: `catalog.get("/schema")`        | Appropriate; no datapath equivalent for schema introspection           |
| `query.py`   | Raw HTTP: caller-supplied path expression | Appropriate; datapath cannot express arbitrary user-supplied traversal |
| `hatrac.py`  | Raw HTTP: `HatracStore` methods           | Appropriate; no datapath equivalent for object store                   |

#### 5.5.2 Annotation Tools (`tools/annotation.py`) [DONE]

16 tools registered. Read tools: `get_table_annotations`, `get_column_annotations`,
`list_foreign_keys`, `get_handlebars_template_variables`. Write tools apply immediately
via `getCatalogModel()` + dict mutation + `model.apply()` (no staged model):
`set_display_annotation`, `set_table_display_name`, `set_row_name_pattern`,
`set_column_display_name`, `set_visible_columns`, `add_visible_column`,
`remove_visible_column`, `set_visible_foreign_keys`, `add_visible_foreign_key`,
`remove_visible_foreign_key`, `set_table_display`, `set_column_display`.

Key difference from deriva-mcp: no `apply_annotations()` step; changes are immediate.
Fine-grained splice tools (`add_visible_column`, etc.) retained for LLM discoverability.

#### 5.5.3 Schema/DDL Tools (`tools/schema.py`) [DONE]

5 tools: `create_table`, `add_column`, `set_table_description`, `set_column_description`,
`set_column_nullok`. Uses `ermrest_model` API directly (Column.define, Table.define,
ForeignKey.define, Schema.create_table, Table.create_column, Table/Column.alter).
All fire `fire_schema_change()` on success.

#### 5.5.4 Vocabulary Tools (`tools/vocabulary.py`) [DONE]

5 tools: `list_vocabulary_terms`, `lookup_term`, `add_term`, `update_term`, `delete_term`.
Uses datapath API (`getPathBuilder()`) directly -- no deriva-ml dependency needed.
`add_term` passes `defaults={"ID","URI"}` so ERMrest auto-generates ID and URI from Name.
`lookup_term`: server-side Name filter first, then client-side Synonyms array scan.
`delete_term`: no pre-flight FK check; ERMrest constraint rejection surfaces the error.

#### 5.5.5 deriva-ml Dependency Decision [RESOLVED]

No deriva-ml code was copied. All three modules were implemented using `ermrest_model`
and the datapath API directly. No utility routines from deriva-ml were needed.

---

### Phase 6 -- Documentation and Handoff [TODO]

- README: installation, transport modes, configuration reference, plugin authoring guide
- Plugin authoring guide:
    - How to write a `register(ctx)` function and package entry points
    - When to use `get_catalog()` / `get_hatrac_store()` vs `get_request_credential()`
    - How to register and use lifecycle hooks (`on_catalog_connect`, `on_schema_change`)
    - How to extend the RAG subsystem: implementing the `VectorStore` protocol, adding
      custom documentation sources, building a data indexing layer (deriva-ml pattern)
    - Testing patterns for plugins (using `conftest.py` fixtures from core test suite)
- Deployment guide: VM, Docker Compose, Kubernetes patterns; ChromaDB vs pgvector
  backend selection guidance
- Note: deriva-ml tool port is out of scope; the plugin framework and lifecycle hooks
  are the handoff artifacts for that work

---

## Out of Scope

- Backward compatibility with `deriva-mcp` prototype
- Port of deriva-ml domain-specific tools (Dataset, Execution, ML workflow management)
  -- separate effort, handed to deriva-ml developer; the plugin framework and lifecycle
  hooks are the handoff artifacts
- Schema-aware data indexing for DerivaML-specific tables (Dataset, Execution, ML workflows)
  -- belongs in deriva-ml plugin; core provides generic `index_table_data()` primitives and
  the `RowSerializer` protocol as the integration point
- Generic annotation/schema/vocabulary tools are now IN scope (see Phase 5.5) since these
  do not depend on DerivaML-specific logic
- Refresh token handling (Credenza derived sessions are fixed-lifetime, 30 min cap)
- Dynamic client registration
- JWKS / JWT token validation (Credenza issues opaque tokens only)
- ChromaDB-to-pgvector migration tooling (operator concern, not core deliverable)