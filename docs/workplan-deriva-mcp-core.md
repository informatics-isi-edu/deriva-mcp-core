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

### Request Auth Flow (HTTP, allow-anonymous mode)

When `DERIVA_MCP_ALLOW_ANONYMOUS=true` the FastMCP `token_verifier`/`auth` parameters
are not set. Instead, `AnonymousPermitMiddleware` (added by `build_http_app()`) handles
all auth decisions:

```
Request with Authorization: Bearer <token>
  -> AnonymousPermitMiddleware: token present
       -> verifier set: CredenzaTokenVerifier.verify_token() (same flow as above)
            -> invalid: 401
            -> valid: contextvars set; tool executes with derived credential
       -> verifier not set (anonymous-only mode, no credenza_url): 401

Request with no Authorization header
  -> AnonymousPermitMiddleware: no token
       -> set _current_credential = {}  (anonymous DERIVA access)
       -> set _current_user_id = "anonymous"
       -> set _mutation_allowed = False  (anonymous is always read-only)
       -> tool executes; get_catalog() uses empty credential dict
```

`build_http_app(mcp)` must be used instead of `mcp.streamable_http_app()` to ensure
the middleware is active. Normal (non-anonymous) mode: `build_http_app()` delegates
to `mcp.streamable_http_app()` unchanged.

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

| Variable           | Required | Default | Description                                 |
|--------------------|----------|---------|---------------------------------------------|
| `DERIVA_MCP_DEBUG` | No       | `false` | Set to `true` to enable DEBUG-level logging |

**Safety and access control:**

| Variable                             | Required | Default | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
|--------------------------------------|----------|---------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `DERIVA_MCP_DISABLE_MUTATING_TOOLS`  | No       | `true`  | Kill switch for all tools registered with `mutates=True`. Defaults to enabled -- operators must explicitly set `false` to allow catalog writes.                                                                                                                                                                                                                                                                                                                                                                                                       |
| `DERIVA_MCP_ALLOW_ANONYMOUS`         | No       | `false` | Allow unauthenticated requests. When `true`, a missing `Authorization` header is treated as anonymous access: DERIVA calls use empty credentials (public/anonymous catalog access) and mutations are blocked regardless of the kill-switch setting. A present but invalid token is still rejected with 401. When `DERIVA_MCP_CREDENZA_URL` is also set, valid tokens are still fully authenticated (mixed mode). When `DERIVA_MCP_CREDENZA_URL` is not set (anonymous-only mode), no Credenza fields are required and any provided token is rejected. |
| `DERIVA_MCP_PLUGIN_ALLOWLIST`        | No       | --      | Comma-separated list of permitted plugin entry point names. If unset, all discovered plugins load. If set (including empty), only named plugins load; others are logged at WARNING and skipped. Example: `deriva-ml,my-org-plugin`                                                                                                                                                                                                                                                                                                                    |
| `DERIVA_MCP_MUTATION_REQUIRED_CLAIM` | No       | --      | JSON object specifying a token claim that must be satisfied for a principal to execute mutating tools (when kill switch is off). Keys are claim names; values are required scalars or lists (list = OR, multiple keys = AND). Example: `{"groups": ["deriva-mcp-mutator"]}`                                                                                                                                                                                                                                                                           |

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
        │   ├── anonymous.py     # AnonymousPermitMiddleware (DERIVA_MCP_ALLOW_ANONYMOUS mode)
        │   ├── introspect.py    # Credenza POST /introspect client
        │   ├── exchange.py      # Credenza POST /token token_exchange client
        │   ├── token_cache.py   # Smart derived token cache
        │   └── verifier.py      # CredenzaTokenVerifier (mcp TokenVerifier protocol)
        ├── plugin/
        │   ├── __init__.py
        │   ├── api.py           # PluginContext class (tool/resource/prompt + lifecycle hooks)
        │   └── loader.py        # Entry point discovery and registration
        ├── rag/                 # Built-in RAG subsystem
        │   ├── __init__.py      # Thin re-export: exposes register() and get_rag_store() from tools.py
        │   ├── config.py        # RAGSettings (DERIVA_MCP_RAG_* vars)
        │   ├── store.py         # VectorStore protocol + ChromaVectorStore + PgVectorStore
        │   ├── chunker.py       # Markdown-aware document chunking
        │   ├── crawler.py       # GitHub repo crawler (Trees API, SHA change detection)
        │   ├── docs.py          # Documentation source ingestion pipeline
        │   ├── schema.py        # Catalog schema indexing (visibility class isolation)
        │   ├── data.py          # Generic data indexing primitives + RowSerializer protocol
        │   └── tools.py         # register(ctx), get_rag_store(), MCP tool closures
        ├── telemetry/           # Observability
        │   ├── __init__.py      # Re-exports audit_event, init_audit_logger
        │   └── audit/
        │       ├── __init__.py
        │       └── logger.py    # Structured JSON audit log (python-json-logger); syslog or rotating file
        └── tools/               # Built-in DERIVA tools (each has register(ctx))
            ├── __init__.py
            ├── catalog.py       # Schema introspection + catalog admin (create/delete/alias/cite)
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

### Phase 4 -- Core ERMREST Tools and RAG Subsystem [DONE]

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

The RAG module is registered as a built-in plugin via `register(ctx)` in `rag/tools.py`.
`rag/__init__.py` is a thin re-export that exposes `register` and `get_rag_store` from
`rag/tools.py`; all implementation lives in `tools.py`.
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

**MCP tools registered by `rag/tools.py`:**

- `rag_search(query, limit?, hostname?, catalog_id?, doc_type?)` -- semantic search.
  When `hostname` and `catalog_id` are provided, schema search is scoped to the calling
  user's current visibility class hash (read from a per-request cache populated by the
  `on_catalog_connect` hook). Doc search is always global.
- `rag_update_docs(source_name?)` -- incremental docs update (SHA delta).
- `rag_index_schema(hostname, catalog_id)` -- manual schema reindex trigger.
- `rag_status()` -- per-source chunk counts, timestamps, and known schema visibility classes.

**Lifecycle hook wiring:**

```python
# in rag/tools.py register(ctx):
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
- RAG subsystem (4.5) is implemented, unit tested, and validated end-to-end in Phase 5
  (live GitHub crawl, live ChromaDB, live DERIVA catalog). All implementation lives in
  `rag/tools.py`; `rag/__init__.py` is a thin re-export.

Deliverable: All built-in tools implemented and unit tested. Structured audit log and
mutation kill switch in place. RAG subsystem validated end-to-end in Phase 5.

---

### Phase 5 -- Integration and Validation [DONE -- 2026-03-25]

**Goal:** End-to-end validation against live Credenza + DERIVA, and RAG subsystem
validated against real documentation content and a real catalog.

#### 5.1 Integration Tests [DONE -- live validation, not automated]

Automated integration tests against live Credenza are not in scope at this stage.
All validation was done via live deployment and human-in-the-loop testing:

- Full OAuth token lifecycle: introspect -> exchange -> derived token -> ERMrest
- Token expiry and cache refresh cycle validated (exposed and fixed contextvar
  propagation bug; see Phase 5.5 post-fixes below)
- Token verifier rejects expired/invalid tokens (confirmed via Credenza audit logs)
- Schema introspection, entity CRUD, and attribute query tools validated against a
  live DERIVA catalog (2026-03-23)

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

Natural language prompts used (Claude Code, MCP server configured against local
deriva-docker stack, catalog 1, test database present):

**Schema/catalog introspection:**

```
List all the schemas in catalog 1 on localhost
```

```
What tables are in the isa schema of catalog 1 on localhost? Show me their columns.
```

```
Get the full definition for the isa:Dataset table including foreign keys
```

**Entity CRUD:**

```
Show me the first 10 rows from isa:Dataset in catalog 1 on localhost
```

```
Insert a new dataset into isa:Dataset with Title=Smoke Test Dataset and Description=Created during MCP validation
```

```
Update the dataset I just created -- set its Description to Updated during MCP validation
```

(validates sparse PUT; original nullable columns must not be nulled out)

```
Delete the dataset with RID <rid> from isa:Dataset
```

(validates filter enforcement; tool must reject empty-filter deletes)

**Attribute/aggregate queries:**

```
Count how many datasets are in catalog 1 grouped by Status
```

```
Query the attribute path isa:Dataset/Status=released and return Title and RID
```

**RAG:**

```
Search for documentation about ERMrest foreign key path syntax
```

```
Index the schema for catalog 1 on localhost and then search for tables related to experiments
```

```
What does rag_status show for the current documentation index?
```

```
Update the documentation index for ermrest-docs
```

**Annotation tools:**

```
Show me the visible columns configuration for isa:Dataset in catalog 1 on localhost
```

```
Set the display name for the isa:Dataset table to Dataset renamed
```

**Token lifecycle (regression test for stateless_http fix, 2026-03-25):**

Configure CREDENZA_DERIVED_SESSION_MAX_TTL=180 in the deployment, wait 3+ minutes
after the initial MCP connection, then issue a mutating call that requires an
authenticated derived session (a read-only call would succeed via anonymous access
and would not exercise the token path):

```
Update the Description field of a Replicate entry in catalog 1 on localhost to token refresh test
```

Validates that derived token expiry and re-exchange is transparent to the LLM --
the update must succeed without requiring reconnect or reauth.

#### 5.3 Plugin Smoke Test [DONE -- 2026-03-25]

`tests/test_plugin_smoke.py` -- 11 tests covering the full plugin authoring contract:

- **Unified pattern**: one `register(ctx)` declares primary tools (read + write) and RAG
  components (documentation source via `ctx.rag_source()` + data-indexing lifecycle hook)
- **Split pattern**: two separate register functions on the same `PluginContext` --
  `_register_tools_only(ctx)` and `_register_rag_only(ctx)` -- produce identical state,
  validating that tools and RAG can ship from separate packages/repos
- Tool execution: read tool returns correct rows (mocked catalog); write tool calls
  `path.update()` and returns success payload
- Kill switch: `disable_mutating_tools=True` blocks write tool without touching catalog
- Lifecycle hook: `fire_catalog_connect()` triggers hook with correct args; split-pattern
  hook fires the same way; hook exceptions are suppressed (never surface to caller)
- `ctx.tool()` without `mutates=` raises `TypeError` at registration time

Implementation note documented in the test file: tool functions must import
`get_catalog` (and `deriva_call`) inside the function body, not at the top of
`register()`. Outer-scope imports are captured as closures at registration time --
before any test patch is applied. Inner imports resolve at call time and pick up
patched values correctly. This is the correct pattern for real plugin authors too.

#### 5.4 Docker Compose Configuration [DONE -- in deriva-docker]

Live deployment uses `deriva-docker` compose stack (Traefik, Apache, Credenza, Keycloak,
deriva-mcp-core). All required env vars documented and tested in that context.
`/health` confirmed suitable for Docker health probes.
A standalone compose config (deriva-mcp-core + Credenza only) is not yet documented
but is low priority given the deriva-docker reference deployment exists.

Deliverable: Full integration validation complete. Reference deployment operational.

---

### Phase 5.5 -- Tool Parity Extension [DONE -- 2026-03-24]

**Goal:** Close the tool coverage gap between `deriva-mcp-core` and the `deriva-mcp`
prototype by adding annotation, schema/DDL, and vocabulary tools.

**Completed:** All three tool modules implemented, registered, and unit tested.
Test count: 234 passing, 6 skipped (up from 181 before this phase).

Post-5.5 improvements also completed (2026-03-24/25):

- `stateless_http=True` set in `FastMCP` constructor (server.py). In stateful mode the
  MCP lowlevel server spawns one long-lived task per session, copying the asyncio context
  at spawn time. When the derived token expires and `verify_token()` exchanges T1->T2, T2
  is set in the middleware coroutine's context but the long-lived task and all child tasks
  it spawns still carry T1 -- so ERMrest receives the expired token. With stateless mode
  each HTTP request spawns a fresh task inside the auth middleware, inheriting the correct
  per-request credential. Validated against live deployment (2026-03-25).
- `_is_401()` in context.py extended to inspect `exc.caused_by`: the deriva-py datapath
  API wraps HTTP errors in `DataPathException` with the original `HTTPError` on `caused_by`;
  the old single-attribute check missed these.

Post-5.5 improvements also completed (2026-03-24):

- `get_catalog(hostname, catalog_id)` replaces `get_deriva_server(hostname).connect_ermrest(catalog_id)`
  across all tool modules, context.py, rag/tools.py, and tests. `get_deriva_server` removed entirely.
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

| Module       | API Strategy                                         | Status                                                                 |
|--------------|------------------------------------------------------|------------------------------------------------------------------------|
| `entity.py`  | Raw HTTP: `/entity/...@sort(RID)@after(rid)?limit=N` | Switched from datapath in Phase 5.6; see note below                    |
| `catalog.py` | Raw HTTP: `catalog.get("/schema")`                   | Appropriate; no datapath equivalent for schema introspection           |
| `query.py`   | Raw HTTP: caller-supplied path expression            | Appropriate; datapath cannot express arbitrary user-supplied traversal |
| `hatrac.py`  | Raw HTTP: `HatracStore` methods                      | Appropriate; no datapath equivalent for object store                   |

Note on `entity.py` switch: `get_entities` was originally datapath-based but the datapath
`_ResultSet` has no `@after` cursor support. Switching to raw HTTP allows `@sort(RID)` and
`@after(rid)` in a single URL, giving consistent row ordering and cursor-based pagination
directly in `get_entities` without requiring the caller to switch to `query_attribute` for
subsequent pages. `insert_entities`, `update_entities`, and `delete_entities` remain
datapath-based: `update_entities` uses `PUT /attributegroup` for sparse updates (datapath
`path.update()` is the only clean way to drive this endpoint); insert and delete follow
the same pattern for consistency.

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

### Phase 5.6 -- Core Gap Closure [DONE -- 2026-03-27]

**Goal:** Close the gaps between the prototype and core that belong in the core platform --
tools with no `deriva-ml` dependency that improve LLM ergonomics or cover missing DERIVA
primitive operations. Informed by [gap_analysis.md](gap_analysis.md).

Each sub-phase is independently mergeable. All new tools follow existing conventions:
`register(ctx: PluginContext)` closure pattern, explicit `mutates=`, `deriva_call()` wrapper
where applicable, audit events on mutating tools, ASCII-only docstrings.

#### 5.6.1 Query Ergonomics (`tools/query.py`)

**`count_table(hostname, catalog_id, schema, table, filters?)`** -- MCP shorthand that
issues an ERMrest aggregate query (`@aggregate/cnt=RID`) and returns a plain integer count.
Optional `filters` is a dict of `{column: value}` equality constraints appended to the
URL as ERMrest filter predicates. Equivalent to calling `query_aggregate` with a
`cnt:=cnt(RID)` aggregate, but with a much simpler interface for the common case.

Registered with `mutates=False`. No audit event needed (read-only).

Tests: `tests/test_tools.py` -- mock catalog, verify URL construction for the no-filter
and single-filter cases; verify integer returned in response.

#### 5.6.2 Annotation Completeness (`tools/annotation.py`)

Five tools covering the annotation ergonomics gap identified in the gap analysis.

**`reorder_visible_columns(hostname, catalog_id, schema, table, context, new_order)`** --
Read the current `visible-columns` annotation for the given context, apply the caller's
`new_order` (list of integer indices or column specs), and write back immediately via
`model.apply()`. `new_order` as integers is interpreted as positional reordering; as column
specs it is a direct replacement of the list order.

**`reorder_visible_foreign_keys(hostname, catalog_id, schema, table, context, new_order)`** --
Same pattern for the `visible-foreign-keys` annotation.

**`get_table_sample_data(hostname, catalog_id, schema, table, limit?)`** -- Fetch a small
number of rows (default 3, max 10) from a table via ERMrest for use in testing Handlebars
templates. Returns rows as a list of dicts. Uses `get_entities` internally.

**`preview_handlebars_template(template, data)`** -- Render a Handlebars template string
using the provided `data` dict. Uses the `chevron` library (add to optional `dev` extra or
a new `annotation` extra). Returns the rendered string or a structured error on parse/render
failure.

**`validate_template_syntax(template)`** -- Validate a Handlebars template for common
syntax errors (unmatched braces, unclosed blocks). Uses `chevron`. Returns
`{"valid": true}` or `{"valid": false, "errors": [...]}`.

Dependency note: `chevron` is a pure-Python Handlebars renderer. Add to `pyproject.toml`
as an optional dependency under `[project.optional-dependencies] annotation = ["chevron"]`
and import lazily inside the tool functions so the core server starts without it when not
installed. Alternatively add to the base dependencies if the footprint is acceptable
(chevron is ~10 KB, no transitive deps).

All five registered with `mutates=False` (reorder tools write annotations but the mutation
kill switch should not block annotation management -- use `mutates=True` for the two write
tools). Reorder tools emit audit events (`annotation_reorder_visible_columns`,
`annotation_reorder_visible_foreign_keys`).

Correction: `reorder_visible_columns` and `reorder_visible_foreign_keys` write to the
catalog, so they must be `mutates=True` and must emit audit events.

Tests: extend `tests/test_annotation_tools.py` (or equivalent) to cover each new tool
with mocked `getCatalogModel()`.

#### 5.6.3 Vocabulary Completeness (`tools/vocabulary.py`)

**`create_vocabulary(hostname, catalog_id, schema, vocabulary_name, comment?)`** --
Create a new vocabulary table in the specified schema using the ERMrest model API
(`ermrest_model.Table.define` with the standard vocabulary column set: `Name`, `URI`,
`Synonyms`, `Description`, `ID`). Fires `fire_schema_change()` on success.

This is a pure `ermrest_model` operation -- no `deriva-ml` dependency. The standard
vocabulary column pattern is well-defined in the DERIVA spec.

**`add_synonym(hostname, catalog_id, schema, vocabulary, term_name, synonym)`** --
Read the term via the datapath API, append the new synonym to the `Synonyms` array,
write back via `update_entities`. Fine-grained tool for LLM ergonomics (avoids
read-before-write at the LLM prompt level).

**`remove_synonym(hostname, catalog_id, schema, vocabulary, term_name, synonym)`** --
Same pattern: read, filter out the synonym, write back.

**`update_term_description(hostname, catalog_id, schema, vocabulary, term_name, description)`** --
Targeted description-only update. More discoverable than `update_term` for this common
case.

`create_vocabulary` is `mutates=True` with audit event. The synonym/description tools
are `mutates=True` with audit events.

Tests: mock catalog and model for `create_vocabulary`; datapath mocks for the synonym
and description tools.

#### 5.6.4 Catalog Admin Tools (`tools/catalog.py`)

Server-level catalog management operations added to the existing `catalog.py` module
(not a separate file). Use the `DerivaServer` and `ErmrestCatalog` APIs directly
without any `deriva-ml` dependency.

**`create_catalog(hostname, schema_name?)`** -- Create a new empty ERMrest catalog via
`DerivaServer.create_ermrest_catalog()`. If `schema_name` is provided, creates that schema
inside the new catalog before returning. Returns the new `catalog_id` and an optional
`catalog_alias` if one is passed. No `deriva-ml` dependency -- this is a plain catalog
creation with no ML schema initialization; the caller (or a plugin) is responsible for any
further schema setup. `mutates=True`. Audit event `catalog_create`.

**`delete_catalog(hostname, catalog_id)`** -- DELETE `/ermrest/catalog/{catalog_id}`.
Permanently destructive. `mutates=True`. Audit event `catalog_delete`.

**`create_catalog_alias(hostname, alias_name, catalog_id, name?, description?)`** --
Create an ERMrest alias via `DerivaServer.create_ermrest_alias()`. `mutates=True`.
Audit event `catalog_alias_create`.

**`update_catalog_alias(hostname, alias_name, alias_target?, owner?)`** --
Update alias target or owner ACL. `mutates=True`. Audit event `catalog_alias_update`.

**`delete_catalog_alias(hostname, alias_name)`** -- Delete alias (not the target catalog).
`mutates=True`. Audit event `catalog_alias_delete`.

**`cite(hostname, catalog_id, rid, current?)`** -- Generate a permanent citation URL for
a catalog entity. `current=False` (default) includes a catalog snapshot timestamp for
reproducibility; `current=True` returns the live URL. `mutates=False`. No auth required
for URL construction -- this is a pure string operation using the hostname and RID.

Register `catalog_admin` in `server.py` alongside the other built-in modules.

Tests: mock `DerivaServer` for alias CRUD; verify URL format for `cite`.

#### 5.6.5 RAG Management Tools (`rag/tools.py`)

Three tools extending the existing RAG tool set for operator and LLM control over the
documentation index.

**`rag_ingest(source_name?)`** -- Force a full re-crawl and reindex of one or all
documentation sources, ignoring SHA change detection. Use when the incremental update
(`rag_update_docs`) missed a change or when a full rebuild is needed. Runs inline
(awaited). Returns per-source chunk counts. `mutates=False` (writes to vector store,
not the DERIVA catalog).

**`rag_add_source(name, repo_owner, repo_name, branch?, path_prefix?, doc_type?)`** --
Register a new documentation source at runtime and immediately trigger an incremental
update for it. Persists the source to `~/.deriva-mcp/rag/sources.json` so it survives
restarts. Sources added via this tool are merged with plugin-declared sources at startup
(plugin-declared takes precedence on name conflict). `mutates=False`.

**`rag_remove_source(name)`** -- Remove a runtime-added source from `sources.json` and
delete all its indexed chunks from the vector store. Cannot remove built-in or
plugin-declared sources (returns an error if attempted). `mutates=False`.

Implementation note: `sources.json` persistence requires a simple JSON read/write helper
in `rag/docs.py`. The `RAGDocsManager` should expose `add_source()` and `remove_source()`
methods that update the in-memory source list and the JSON file atomically (write to temp
then rename).

Tests: mock vector store and filesystem for add/remove; verify `sources.json` contents;
verify `rag_ingest` calls `update()` with `force=True`.

#### 5.6.6 Catalog-Level Chaise Annotation (`tools/annotation.py`)

**
`apply_navbar_annotations(hostname, catalog_id, navbar_brand_text?, head_title?, default_table?, navbar_menu?, auto_schema_menu?, show_system_columns?)`
** --
Set the `chaise-config` and `display` annotations at the catalog object level, controlling
how the Chaise web interface presents the catalog: navbar brand text, browser tab title,
default landing table, system column visibility, and an optional navigation bar menu.

Standard settings applied on every call: `underline_space`, `deleteRecord`,
`showFaceting`, `shareCiteAcls`, `exportConfigsSubmenu` (accessible to all users).

Navbar menu options (mutually exclusive; `navbar_menu` takes precedence):

- `navbar_menu`: caller-supplied full `navbarMenu` dict passed through as-is
- `auto_schema_menu=True`: builds a simple menu from the live schema -- one submenu per
  non-public schema with all tables listed alphabetically; useful for quick setup

This covers the generic portion of the prototype's `apply_catalog_annotations`. The
ML-specific menu generation (Workflow, Execution, Dataset groupings; vocabulary/asset/
feature detection) belongs in the `deriva-ml` plugin and is not in scope for core.

`mutates=True`. Audit events `annotation_apply_navbar` and `annotation_apply_navbar_failed`.

Tests: basic, `default_table`, `navbar_menu`, `auto_schema_menu`, `show_system_columns=False`,
error+audit paths -- 6 tests.

Status notes (2026-03-26/27):

All sub-phases (5.6.1-5.6.6) implemented and tested. Additional improvements
completed in the same commit window:

- `clone_catalog` added to `catalog.py` (was listed in gap analysis §3.7 as needing
  `deriva-ml`; turned out to be a pure `ErmrestCatalog.clone_catalog()` call with no
  `deriva-ml` dependency).
- `resolve_snaptime` bugs fixed: (a) Crockford decoder silently accepted ISO date strings
  like `"2022-05-12"` -- fixed by `_looks_like_snaptime()` guard (checks for uppercase
  Crockford letters before attempting decode); (b) `catalog.get("")` hit the wrong URL on
  `ErmrestSnapshot` objects whose `_server_uri` has no trailing slash -- fixed to
  `catalog.get("/")`.
- Snaptime format in all `catalog_id` docstrings updated to show dashed format
  (`2TA-YA2D-ZDWY`) and warn against hand-constructing `ID@snaptime` with plain dates.
- `get_entities` enhanced: (a) `preflight_count=True` returns row count only, never
  fetches entities, with `action_required` guidance; (b) `after_rid` cursor parameter;
  (c) switched from datapath to raw HTTP `/entity/...@sort(RID)@after(rid)?limit=N`
  so ordering is deterministic and pagination is cursor-based without switching tools.
- `query_attribute` enhanced: `after_rid` and `limit` parameters added; `@sort(RID)@after(rid)`
  is appended after the column projection in the URL (required by ERMrest URL grammar).

---

### Phase 5.6.7 -- Security Hardening [DONE -- 2026-03-27]

Two operator-configurable controls that harden the plugin and mutation surface without
changing default behavior for existing deployments.

#### Plugin allowlist (`DERIVA_MCP_PLUGIN_ALLOWLIST`)

Entry-point discovery is open by default: any package installed in the server's Python
environment that declares the `deriva_mcp.plugins` entry point is loaded automatically.
This is convenient but means a compromised or malicious package can inject code by
declaring the entry point -- with no second gate if hot-reload or automatic plugin
activation were ever added.

`DERIVA_MCP_PLUGIN_ALLOWLIST` (comma-separated entry point names) restricts loading to
the named set. If unset, all discovered plugins load (existing behavior preserved). An
explicitly empty value disables all external plugins. Skipped plugins are logged at
`WARNING` so operators can audit what was blocked.

Implementation: `load_plugins(ctx, allowlist)` in `plugin/loader.py`; `plugin_allowlist`
field in `Settings`; wired through `create_server()`.

#### Per-user mutation claim gating (`DERIVA_MCP_MUTATION_REQUIRED_CLAIM`)

When the mutation kill switch is off, all authenticated users can execute mutating tools. This
adds a finer-grained control: a JSON claim spec (e.g. `{"groups": ["deriva-mcp-mutator"]}`)
that must be satisfied by the token introspection payload. Users whose token lacks the
required claim receive `{"error": "catalog mutations are not permitted for your account"}`
and a `mutation_claim_denied` audit event. The kill switch takes precedence when both
are active. stdio mode is unaffected (`_mutation_allowed` defaults to `True`).

Matching semantics: list required values use OR (any one match sufficient); multiple
keys in the spec use AND (all must match). Handles scalar claims (`{"mcp_can_mutate": true}`),
list claims (`{"groups": ["a", "b"]}`), and cross-type matching (scalar required vs.
list actual and vice versa).

Implementation: `_satisfies_claim_spec()` in `auth/verifier.py`; `_mutation_allowed`
contextvar + `set_mutation_allowed()` / `is_mutation_allowed()` in `context.py`;
`mutation_required_claim` field in `Settings`; `PluginContext.__init__` takes
`mutation_required_claim`; guarded wrapper in `plugin/api.py` checks both kill switch
and claim at call time; `create_server()` passes the setting through.

Tests: 16 new tests across `test_verifier.py` (claim spec matching + contextvar setting)
and `test_plugin.py` (guard behavior: allowed, denied, read tools unaffected, kill switch
precedence, no-config fast path).

---

### Phase 5.7 -- Background Task Infrastructure [DONE]

**Goal:** Add a server-level background task system to core so that long-running operations
(catalog clone, RAG bulk ingest, schema migrations, dataset export) can be submitted as
async tasks, returning a `task_id` immediately. Plugins use a single shared primitive
rather than each implementing their own task tracking. Three built-in MCP tools let the
LLM poll for status and cancel tasks.

This is a platform primitive, not a domain concern. Centralizing it in core prevents
inconsistent per-plugin implementations.

#### 5.7.1 Task Manager (`tasks/`)

New module `tasks/` with the following structure:

```
tasks/
  __init__.py    # re-exports: TaskManager, submit_task, get_task_manager
  manager.py     # TaskManager class
```

**`TaskManager`** -- server-level singleton, initialized at startup in `server.py`.

Task lifecycle states: `pending`, `running`, `completed`, `failed`, `cancelled`.

Each task entry stores:

```python
@dataclass
class TaskRecord:
    task_id: str  # UUID4
    principal: str  # iss/sub of submitting user; "stdio" in stdio mode
    name: str  # human-readable task name
    description: str  # optional longer description
    state: str  # pending | running | completed | failed | cancelled
    created_at: str  # ISO timestamp
    started_at: str | None
    completed_at: str | None
    result: Any  # JSON-serializable; set on completion
    error: str | None  # error message on failure
    progress: str | None  # optional free-form progress string; updated by task coroutine
```

**`TaskManager` API:**

- `submit(coroutine, name, principal, description?) -> str` -- wrap `coroutine` in a task wrapper
  that updates state machine, emit `task_submitted` audit event, call
  `asyncio.create_task(wrapper)`, return `task_id`
- `get(task_id, principal) -> TaskRecord | None` -- return record if it belongs to
  `principal`, else `None`
- `list(principal, status?) -> list[TaskRecord]` -- all tasks for principal, optionally
  filtered by state
- `cancel(task_id, principal) -> bool` -- call `asyncio.Task.cancel()` if still running;
  return True if cancellation was requested

Task wrapper logic:

```python
async def _run_task(record, coroutine):
    record.state = "running"
    record.started_at = now()
    try:
        record.result = await coroutine
        record.state = "completed"
    except asyncio.CancelledError:
        record.state = "cancelled"
        raise
    except Exception as exc:
        record.error = str(exc)
        record.state = "failed"
    finally:
        record.completed_at = now()
        audit_event(f"task_{record.state}", task_id=record.task_id, name=record.name,
                    principal=record.principal)
```

**Storage: in-memory only.** Tasks are lost on server restart. Persistence (JSON or SQLite)
is deferred -- the API is forward-compatible because `TaskRecord` is a plain dataclass
that serializes to JSON. Add `--task-persist` as a future config option without changing
the public API.

**Principal scoping:** `submit()` requires `principal` explicitly (the caller reads it from
the contextvar at submission time). This prevents a task submitted in one request from
being visible to a different user. In stdio mode, pass `get_request_user_id()` which
returns `"stdio"`.

**Credential lifetime and task duration:**

The derived token is capped at 30 minutes (Credenza `SessionType.DERIVED`). For tasks
that outlive a single derived token, the `TaskManager` must be able to re-exchange on
demand. To support this, `submit()` captures both the `principal` and the original
`bearer_token` from the HTTP request.

A new `_current_bearer_token: ContextVar[str | None]` is added to `context.py` alongside
`_current_credential`, set at HTTP request start and readable via `get_request_bearer_token()`.
The `TaskManager` stores `(principal, bearer_token)` in a private mapping keyed by `task_id`
(separate from `TaskRecord` to avoid accidental serialization if persistence is added later).

**`TaskManager` gains one additional method:**

- `get_credential(task_id) -> dict` -- async; calls `DerivedTokenCache.get(principal, bearer_token)`
  to obtain a fresh derived credential, re-exchanging if near expiry. Long-running task
  coroutines call this before each batch of DERIVA operations rather than holding a captured
  snapshot.

**Effective task lifetime = bearer token TTL.** The primary session bearer token is
currently configured at 24 hours, which covers realistic task durations (catalog clone,
bulk RAG ingest, ML training runs). If the bearer token expires mid-task, `get_credential`
raises, the task fails with an auth error, and `record.state = "failed"` with an appropriate
error message.

**Device flow + offline_access:** Obtaining a refresh token via device flow would allow
indefinite credential renewal, but this path is not viable at the current MCP transport
layer. FastMCP only sees the Bearer access token in the HTTP Authorization header -- the
refresh token lives in the MCP client and is never sent to the server. For the server to
benefit, the refresh token would need to be passed explicitly at task submission time, which
is a non-standard and security-sensitive pattern. Defer until there is a concrete need.

**Future path if 24-hour tokens are insufficient:** Credenza can issue a task-scoped token
at submission time with a configurable TTL via `token_exchange`. This requires no client-side
changes and fits the existing token exchange model. Defer until a concrete use case arises.

#### 5.7.2 PluginContext Extension (`plugin/api.py`)

Add `submit_task(coroutine, name, description?) -> str` to `PluginContext`. This reads the
current principal and bearer token from contextvars (must be called within a tool handler)
and delegates to `_task_manager.submit(coroutine, name, principal, bearer_token, description)`.
Returns the `task_id`.

```python
# Example usage inside a tool handler -- async task:
task_id = ctx.submit_task(
    _do_clone(hostname, src_id, dst_id),
    name=f"clone_catalog {src_id} -> {dst_id}",
)
return json.dumps({"task_id": task_id, "status": "submitted"})
```

The running coroutine calls `task_manager.get_credential(task_id)` before each DERIVA
operation to get a fresh derived credential (re-exchanged automatically if near expiry).

**Synchronous plugin code:** Many deriva-ml operations (training loops, data processing
pipelines) are synchronous and cannot be submitted as coroutines directly without blocking
the entire event loop. Wrap them with `asyncio.to_thread()`:

```python
# Synchronous work -- wrap in an async coroutine first so credential
# refresh (async) and thread dispatch can be composed cleanly:
async def _run_training_task(task_id, dataset_path, params):
    cred = await task_manager.get_credential(task_id)
    result = await asyncio.to_thread(run_training_sync, dataset_path, params, cred)
    return result


task_id = ctx.submit_task(
    _run_training_task(...),
    name="model_training",
)
```

`asyncio.to_thread()` runs the callable in a thread pool executor without blocking the
event loop. Capture any credential before entering the thread; `get_credential` is async
and cannot be called from inside the thread directly. The async wrapper pattern above
handles this correctly: fetch credential in async context, pass as plain value into thread.

Plugins that don't use the `PluginContext` wrapper (e.g., they call `submit_task` from
a lifecycle hook) import `get_task_manager()` from `deriva_mcp_core.tasks` and call
`manager.submit(coroutine, name, principal, bearer_token, description)` directly, reading
the principal and bearer token from `get_request_user_id()` and `get_request_bearer_token()`.

#### 5.7.3 Built-in MCP Tools (`tools/tasks.py`)

New module `tools/tasks.py` registered from `server.py`. Three tools, all `mutates=False`
(task management does not write to the DERIVA catalog):

**`get_task_status(task_id)`** -- Return the full `TaskRecord` for the given task if it
belongs to the calling principal. Returns `{"error": "not found"}` for unknown or
other-principal task IDs (no information leakage).

**`list_tasks(status?)`** -- Return all tasks for the calling principal. Optional `status`
filter (`"running"`, `"completed"`, `"failed"`, `"cancelled"`, `"pending"`). Returns
tasks sorted by `created_at` descending.

**`cancel_task(task_id)`** -- Request cancellation of a running task. Returns
`{"cancelled": true}` if the request was accepted, `{"cancelled": false, "reason": "..."}`
if the task is already completed or does not belong to the caller.

All three registered with `mutates=False`. No audit events needed beyond what the task
wrapper already emits.

#### 5.7.4 `clone_catalog_async` (`tools/catalog.py`)

Add `clone_catalog_async` alongside the existing synchronous `clone_catalog`. Takes the
same parameters plus an optional `task_name` override. Captures the credential before
submitting, then calls `ctx.submit_task(coroutine, name)`.

```python
@ctx.tool(mutates=True)
async def clone_catalog_async(
        hostname: str,
        source_catalog_id: str,
        dest_catalog_id: str | None = None,
        copy_data: bool = True,
        copy_annotations: bool = True,
        copy_policy: bool = True,
        exclude_schemas: list[str] | None = None,
        name: str | None = None,
        description: str | None = None,
) -> str:
    """Submit a catalog clone as a background task. Returns a task_id immediately.

    Use get_task_status(task_id) to poll for completion. For small catalogs,
    prefer clone_catalog (synchronous) to avoid the polling overhead.
    """
    task_id = ctx.submit_task(
        _clone_coroutine(hostname, source_catalog_id, dest_catalog_id, copy_data,
                         copy_annotations, copy_policy, exclude_schemas, name,
                         description),
        name=f"clone_catalog {source_catalog_id}",
    )
    audit_event("catalog_clone_async_submitted", hostname=hostname,
                source_catalog_id=source_catalog_id, task_id=task_id)
    return json.dumps({"task_id": task_id, "status": "submitted"})
```

The inner coroutine `_clone_coroutine` receives `task_id` and calls
`task_manager.get_credential(task_id)` before each DERIVA operation so that derived token
re-exchange happens automatically if the clone runs longer than 30 minutes. A 24-hour
bearer token window means the full clone can run without interruption under normal
conditions.

#### 5.7.5 `rag_update_docs_async` (`rag/tools.py`) [DONE]

`rag_update_docs` runs inline and can be slow for a large documentation corpus. Add
`rag_update_docs_async(source_name?)` that submits the same work via `ctx.submit_task`.
Returns `task_id` immediately. The LLM polls `get_task_status` to confirm completion.

No credential capture needed for RAG operations -- the vector store uses a service-level
connection, not a per-request credential.

Both `rag_update_docs` and `rag_update_docs_async` were updated to accept `force: bool = False`.
When `force=True`, both tools call `docs_manager.ingest(src, force=True)` which bypasses
the SHA delta cache and re-fetches all files. This eliminates the need for a separate
`rag_ingest` call when a forced full rebuild is needed.

#### 5.7.6 Tests [DONE]

- `tests/test_tasks.py` -- 20 unit tests for `TaskManager`: submit, get, list, cancel,
  principal isolation (task from principal A not visible to principal B), state machine
  transitions, cancellation of already-completed task returns False, get_credential with
  mock token cache, update_progress, description field
- `tests/test_tools.py` -- 7 task tool tests: `get_task_status` not-found and found,
  `list_tasks` empty/status-filter/invalid-status, `cancel_task` accepted and rejected
- `tests/test_tools.py::TestCatalogTools` -- 2 `clone_catalog_async` tests: happy path
  (patches kept live during background task execution; asserts task completes with correct
  dest_catalog_id), and submit error when TaskManager not configured
- `tests/test_rag_tools.py::TestRagUpdateDocs` -- 2 additional tests for `force` param:
  confirms `ingest` called with `force=False` by default, and `force=True` when passed

Test count: 433 passing, 6 skipped, 91% coverage (2026-03-27).

Deliverable: Background task infrastructure in core. `clone_catalog_async` and
`rag_update_docs_async` as the first two consumers. Plugin authors use
`ctx.submit_task()` for any long-running operation.

#### 5.7.7 Future: Task State Persistence [TODO]

Task state is currently in-memory only and is lost on server restart. The `TaskRecord`
dataclass is intentionally JSON-serializable (all fields are plain types) so that
persistence can be added later without changing the public API.

Planned approach when a concrete use case arises:

- Add `DERIVA_MCP_TASK_PERSIST_PATH` (filesystem path) or
  `DERIVA_MCP_TASK_PERSIST_BACKEND` (`sqlite` or `postgresql`) to `Settings`
- `TaskManager.__init__` loads existing records from the backend on startup
- `_run_task` flushes state changes (pending -> running -> completed/failed/cancelled)
  to the backend after each transition
- Completed/failed records are retained for a configurable TTL then purged
- The `TaskRecord.task_id` (UUID4) and `TaskRecord.principal` fields ensure
  records are portable across restart and remain principal-scoped

No schema changes to `TaskRecord` are needed; the dataclass already covers all
persistence-relevant fields. The `_credentials` dict (principal, bearer_token pairs)
is deliberately kept separate from `TaskRecord` and must NOT be persisted -- bearer
tokens must not be written to disk.

---

### Phase 6 -- Documentation and Handoff [DONE -- 2026-03-30]

- README: installation, transport modes, configuration reference, plugin authoring guide **[DONE]**
- Plugin authoring guide (`docs/plugin-authoring-guide.md`): **[DONE]**
    - How to write a `register(ctx)` function and package entry points
    - When to use `get_catalog()` / `get_hatrac_store()` vs `get_request_credential()`
    - How to register and use lifecycle hooks (`on_catalog_connect`, `on_schema_change`)
    - How to extend the RAG subsystem: custom documentation sources, `index_table_data()`,
      `RowSerializer` protocol (deriva-ml pattern)
    - Background task submission and credential re-exchange
    - Testing patterns for plugins (`_CapturingMCP`, `PluginContext`, import patterns)
- Deployment guide (`docs/deployment-guide.md`): Docker Compose (deriva-docker),
  standalone Docker, VM/bare metal (systemd + reverse proxy configs for Traefik/Apache/nginx),
  ChromaDB vs pgvector selection, logging/audit, TLS, MCP client config **[DONE]**
- Usage guide (`docs/usage-guide.md`): natural language prompt examples for all tool categories **[DONE]**
- Note: deriva-ml tool port is out of scope; the plugin framework and lifecycle hooks
  are the handoff artifacts for that work

---

### Post-Phase-6 Hardening

**Anonymous / zero-auth mode [DONE -- 2026-04-01]**

Added `DERIVA_MCP_ALLOW_ANONYMOUS=true` to support deployments where authentication
is optional or unavailable:

- `AnonymousPermitMiddleware` (`auth/anonymous.py`): ASGI middleware that handles
  all auth decisions when anonymous mode is active. Token present -> validates via
  Credenza as normal; token absent -> sets empty credential and `mutation_allowed=False`;
  invalid token -> 401 (never silently downgraded to anonymous).
- `build_http_app(mcp)` (`server.py`): utility that wraps `mcp.streamable_http_app()`
  with the anonymous middleware when needed. Used by `main()` and tests.
- Two sub-modes controlled by whether `DERIVA_MCP_CREDENZA_URL` is set:
    - Mixed mode (credenza_url set): authenticated and anonymous requests both supported.
    - Anonymous-only mode (no credenza_url): Credenza fields not required at startup;
      any provided bearer token is rejected with 401.
- Anonymous background tasks inherit empty credentials via contextvar propagation;
  `TaskManager` requires no changes.
- 18 new tests in `tests/test_anonymous.py`; full suite: 439 tests, 88% coverage.

**RAG ACL isolation and stdio identity [DONE -- 2026-04-02]**

Fixed a security gap where schema indexing claimed per-ACL-view isolation but
actually served all users the same schema index (the first accessor's view).
Also resolved stdio-mode identity: `get_request_user_id_optional()` returns
None in stdio mode, so schema fetches were silently keyed as anonymous,
defeating per-user isolation.

#### Problem

`_connected_catalogs` was keyed by `(hostname, catalog_id)` -- one entry per
catalog regardless of which user triggered the index. The first user to access
a catalog owned the schema chunk for everyone. A restricted user could receive
schema chunks from a privileged user's view of the same catalog.

Note: ERMrest `/schema` itself is NOT ACL-filtered -- it returns full structural
schema to all authenticated users. The isolation concern is for environments
where schema visibility should still differ per user (e.g., future ACL-filtered
endpoints, or where schema visibility is used as a proxy for catalog access
gating).

#### Fix: per-user schema indexing (`tools/catalog.py`)

- `_connected_catalogs: set[tuple[str, str]]` replaced by
  `_connected_user_catalogs: set[tuple[str, str, str]]` keyed by
  `(hostname, catalog_id, user_id)`.
- `_fetch_schema(hostname, catalog_id, user_id)` -- takes `user_id`, pre-claims
  the triple before the async fetch to prevent concurrent duplicate indexing.
- `_on_catalog_access`: resolves identity via `resolve_user_identity(hostname)`;
  each user's first access to a catalog triggers their own schema fetch.
- All explicit `_fetch_schema` call sites updated to pass
  `resolve_user_identity(hostname)` as `user_id`.

#### Fix: `_user_schema_hashes` and `rag_search` filtering (`rag/tools.py`)

- New module-level `_user_schema_hashes: dict[tuple[str, str, str], str]`
  mapping `(user_id, hostname, catalog_id) -> hash[:16]`.
- Populated in `_handle_catalog_connect` after successful schema indexing, and
  in `rag_index_schema` after manual reindex.
- `rag_search` with `hostname`+`catalog_id` now:
    - Resolves caller's identity via `resolve_user_identity(hostname)`.
    - Looks up `user_hash = _user_schema_hashes.get((user_id, hostname, catalog_id))`.
    - If hash found: keeps only schema results whose `source` exactly equals
      `schema_source_name(hostname, catalog_id, user_hash)`.
    - If hash not found (schema not yet indexed for this user): all schema results
      are excluded -- never serves another user's visibility class.
    - Non-schema results (`doc_type != "schema"`) are unaffected.

#### `resolve_user_identity(hostname)` (`context.py`)

New synchronous public function exported from `deriva_mcp_core`:

- HTTP mode: reads `_current_user_id` contextvar (set by auth middleware) --
  no I/O.
- stdio mode: calls `GET /authn/session` on the DERIVA host using the current
  credential. Parses `id` (non-legacy) or `client.id` (legacy) from the
  response. Result is cached per internal hostname for the server lifetime
  (identity does not change with credential rotation). Uses `requests` (lazy
  import; transitive dep via deriva-py). Falls back to `"anonymous"` on
  failure or missing credential.
- Deliberately synchronous -- it is a fundamental control-flow call in
  stdio mode and the latency is acceptable.

The function is also exported from `deriva_mcp_core.__init__` so plugins can
use it without reaching into `context` directly.

#### Tests

- `test_hostname_catalog_filters_out_other_catalog_schema_results`: seeds
  `rag_tools._user_schema_hashes[("anonymous", "localhost", "1")]` with cleanup.
- New `test_schema_results_excluded_when_hash_not_registered`: verifies all
  schema results are withheld when the caller's hash is not yet registered.
- Full suite: 454 tests, 91% coverage.

---

## Planned: Phase 7 -- Generalized RAG Sources

**Status:** Design only.

### Motivation

The current RAG subsystem indexes only GitHub-hosted Markdown. Two additional
source types are needed for real deployments:

1. **Web crawler sources** -- index the public-facing website for a DERIVA
   deployment (e.g., `https://www.facebase.org`). A prototype of this pattern
   exists in `fb-chatbot/facebase_crawler.py`.

2. **Local filesystem sources** -- ingest pre-processed Markdown or JSON chunk
   files from the local filesystem. Operators who run an offline crawl or
   generate documentation via a CI pipeline can drop the output into a watched
   directory without needing a live HTTP crawl at server startup.

Together these unblock two concrete use cases:

- Indexing the FaceBase public site for a `deriva-facebase` plugin (see below).
- Importing pre-built indexes from any external tool without modifying core code.

---

### 7.1 Web Crawler Source (`rag/web_crawler.py`)

A new `WebCrawler` class alongside `GitHubCrawler`. Uses `httpx` (already a
dep) + `beautifulsoup4` (new dep, add to `rag` extra in `pyproject.toml`).

**`WebSource` dataclass (`rag/docs.py`)**

```python
@dataclass
class WebSource:
    name: str  # unique source identifier
    base_url: str  # crawl root (e.g., "https://www.facebase.org")
    max_pages: int  # crawl limit; None = unlimited
    doc_type: str = "web-content"
    allowed_domains: list[str] = field(default_factory=list)  # empty = base_url domain only
    include_path_prefix: str = ""  # only index URLs under this prefix (optional filter)
```

**`WebCrawler` class (`rag/web_crawler.py`)**

- BFS over the site starting from `base_url`.
- Fetches pages with `httpx.AsyncClient`; respects `Content-Type: text/html`.
- HTML extraction: remove `script`, `style`, `nav`, `header`, `footer`; extract
  title + main content selectors (`main`, `article`, `.content`, etc.); fall
  back to `body`. Same approach as `fb-chatbot/facebase_crawler.py`.
- Dedup by MD5 hash of extracted text -- different URLs with the same content
  (common in Chaise facet URLs) produce one chunk set.
- Recursive path loop detection: skip URLs where any path segment appears more
  than twice, or where path depth exceeds a configurable limit (default 10).
- Rate limiting: `await asyncio.sleep(rate_limit_seconds)` between requests
  (default 1.0 s). Configurable per source.
- Source key: `{source.name}:{url}` for chunk `source` field (parallel to
  `{github_source.name}:{file_path}` for GitHub sources).
- Incremental update: URL-to-content-hash map cached in
  `{source_name}_url_cache.json` in the data dir. Only re-fetches URLs whose
  hash has changed.
- Does NOT respect `robots.txt` by default (operators control what they index);
  add optional `respect_robots: bool = True` flag.

**`RAGDocsManager.ingest_web(source: WebSource, force: bool)`**

Parallel to `ingest()` -- crawl + chunk + upsert. Text extracted from HTML is
chunked with `chunk_markdown()` (works fine on plain prose; treats the extracted
text as a single document). Title is prepended as an H1 to preserve page context.

**Plugin API extension (`plugin/api.py`)**

New `RagWebSourceDeclaration` dataclass + `ctx.rag_web_source()` method:

```python
ctx.rag_web_source(
    name="facebase-web",
    base_url="https://www.facebase.org",
    max_pages=500,
    doc_type="web-content",
)
```

The startup loop in `rag/tools.py` picks up web sources from `ctx._rag_web_sources`
alongside GitHub and local sources.

---

### 7.2 Local Filesystem Source (`rag/local_source.py`)

Ingests `.md` and `.txt` files from a local directory tree.

**`LocalSource` dataclass (`rag/docs.py`)**

```python
@dataclass
class LocalSource:
    name: str
    path: str  # absolute or relative path to directory or single file
    glob: str = "**/*.md"
    doc_type: str = "user-guide"
    encoding: str = "utf-8"
```

**Ingest logic**

- Walk `Path(source.path).glob(source.glob)`.
- Use file mtime (ISO string) as the change-detection key instead of a Git SHA.
- Source key: `{source.name}:{relative_path}`.
- Cached in `{source_name}_mtime_cache.json`.
- Chunk with `chunk_markdown()`.

**Plugin API**

```python
ctx.rag_local_source(
    name="my-site-docs",
    path="/data/docs/exported",
    glob="**/*.md",
    doc_type="user-guide",
)
```

**`rag_update_docs` tool extension**

When a source name is given, `rag_update_docs` dispatches to the right ingest
method based on source type (GitHub, web, or local). The source registry in
`docs_manager` needs to be unified so all three types are looked up by name.

---

### 7.3 `rag_import_chunks` Tool

One-shot bulk import from a pre-built JSON chunk file. Designed for operators
who run an offline crawl or ETL pipeline and produce a standard chunk export.

**Input format** (JSON array):

```json
[
  {
    "text": "...",
    "source": "facebase-web:https://www.facebase.org/about",
    "doc_type": "web-content",
    "chunk_index": 0,
    "metadata": {}
  },
  ...
]
```

**Tool signature**

```python
async def rag_import_chunks(
        file_path: str,
        source_name: str | None = None,  # if provided, override source field in all chunks
        doc_type: str | None = None,  # if provided, override doc_type in all chunks
        replace: bool = False,  # if True, delete_source() before upsert
) -> str:
    ...
```

Returns `{"status": "imported", "chunk_count": N}`.

This directly accommodates the `fb-chatbot` pattern where `facebase_crawler.py`
produces a pre-built index that can be loaded into the core vector store without
re-crawling at runtime.

---

### 7.4 Built-in Prompts and Resources

**Status:** Design only.

#### Motivation

MCP prompts and resources provide a different interaction surface from tools:

- **Resources** are read-accessible URIs -- the LLM can request them directly
  via `read_resource()` without a tool call. Good for relatively stable,
  reference-like data (schema, server config).
- **Prompts** are named workflow guides returned as structured message lists.
  Good for domain workflows where the LLM needs step-by-step guidance rather
  than raw capability.

`PluginContext` already exposes `ctx.resource()` and `ctx.prompt()` pass-through
decorators. Core registers none of its own yet.

#### Built-in resources (`tools/resources.py`, new file)

| URI                                                               | Content                                                 | Notes                                    |
|-------------------------------------------------------------------|---------------------------------------------------------|------------------------------------------|
| `deriva://server/status`                                          | JSON: version, enabled features, auth mode, RAG backend | static at startup                        |
| `deriva://catalog/{hostname}/{catalog_id}/schema`                 | Full ERMrest schema JSON                                | re-fetched on access; uses get_catalog() |
| `deriva://catalog/{hostname}/{catalog_id}/tables`                 | JSON list of `{schema, table, comment}`                 | derived from schema                      |
| `deriva://catalog/{hostname}/{catalog_id}/table/{schema}/{table}` | Table definition JSON (columns, FKs, annotations)       | derived from schema                      |

Resources use the same `get_catalog()` + credential flow as tools. Schema
resources are naturally cached by the RAG subsystem if RAG is enabled.

#### Built-in prompts (`tools/prompts.py`, new file)

| Name              | Description                                                                                |
|-------------------|--------------------------------------------------------------------------------------------|
| `explore-catalog` | Step-by-step guide: list schemas, list tables, inspect a table, run a sample query         |
| `query-catalog`   | Guide to building ERMrest queries: entity path, attribute projection, filters, sort, limit |
| `annotate-table`  | Guide to setting display annotations (visible columns, display names) via annotation tools |

Prompts return a list of `PromptMessage` objects (user + assistant turns) as
expected by the MCP SDK. They do not require tool calls to render -- they are
static workflow text.

Both `tools/resources.py` and `tools/prompts.py` are registered via
`register(ctx)` functions called from `server.py` alongside the existing tool
modules.

---

## Planned: deriva-facebase-mcp-plugin Plugin (Design Note)

**Repo:** `deriva-facebase-mcp-plugin` (separate package, separate repo)

A site-specific plugin for the FaceBase deployment that uses the generalized RAG
primitives from Phase 7 and adds FaceBase-specific tools, resources, and prompts.

Inspired by the proof-of-concept in `fb-chatbot/` (facebase_crawler.py,
facebase_chatbot.py).

### RAG sources

```python
def register(ctx: PluginContext) -> None:
    # Public website content
    ctx.rag_web_source(
        name="facebase-web",
        base_url="https://www.facebase.org",
        max_pages=500,
        doc_type="web-content",
    )
    # Dataset records with vocabulary terms and contributors
    ctx.on_catalog_connect(_index_facebase_datasets)
```

`_index_facebase_datasets` is an `on_catalog_connect` hook that fires when the
FaceBase catalog is first accessed. It runs a variant of `FaceBaseDataBaseCrawler`
using `get_catalog()` + the existing credential flow -- no hardcoded hostname or
credential. It formats each dataset as a Markdown document (same template as
`facebase_crawler.py:FaceBaseDataBaseCrawler.extract_content`) and upserts via
`store.upsert(chunks)`.

This is structurally identical to the existing `_handle_catalog_connect` hook
in `rag/tools.py` that indexes schema -- the same fire-and-forget pattern, but
for dataset records.

### FaceBase-specific prompts

```python
ctx.prompt("facebase-assistant")(...)  # returns the system prompt from facebase_chatbot.py
ctx.prompt("find-datasets")(...)  # step-by-step guide to querying isa:dataset
ctx.prompt("explore-anatomy")(...)  # guide to browsing vocab:anatomy and linked datasets
```

### FaceBase-specific resources

```python
ctx.resource("deriva://facebase/projects")(...)  # list active FaceBase projects
ctx.resource("deriva://facebase/dataset/{rid}")(...)  # dataset detail with vocab terms
ctx.resource("deriva://facebase/anatomy/{name}")(...)  # anatomy term + linked datasets
```

### FaceBase-specific tools (if needed)

Generic `get_entities` + `rag_search` cover most queries. FaceBase-specific
tools would only be added if they handle a pattern that generic tools cannot:
for example, a `get_dataset_summary` tool that fetches the dataset record plus
all associated vocabulary terms in a single formatted response (the multi-join
pattern from `FaceBaseDataBaseCrawler`).

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