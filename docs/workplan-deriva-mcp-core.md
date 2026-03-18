# deriva-mcp-core Workplan

## Overview

`deriva-mcp-core` is a new, MCP server reference implementation for the DERIVA platform.

Design goals:

- Proper per-request OAuth authentication (bearer token validation via Credenza introspection
  and token exchange)
- Core DERIVA primitives (schema introspection, entity CRUD, attribute/aggregate queries,
  Hatrac object store) implemented as built-in tools
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
       -> calls get_deriva_server(hostname)                   [ERMREST tools]
            -> reads _current_credential from contextvar
            -> returns authenticated DerivaServer
            -> tool calls .connect_ermrest(catalog_id) -> ErmrestCatalog
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

- Keyed by `sub` (from introspection response)
- Each entry stores `(derived_token, expires_at)`
- Near-expiry threshold: configurable, default 60 seconds before expiry
- Per-`sub` async lock prevents duplicate exchanges under concurrent requests
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

- `ctx.tool()` -- decorator to register an MCP tool
- `ctx.resource(uri_pattern)` -- decorator to register an MCP resource
- `ctx.prompt(name)` -- decorator to register an MCP prompt

Inside any registered tool or resource, handlers access DERIVA via one of three public
functions imported from `deriva_mcp_core`:

- `get_deriva_server(hostname)` -- returns an authenticated `DerivaServer`; use to obtain
  `ErmrestCatalog` and other bindings:
  `catalog = get_deriva_server(hostname).connect_ermrest(catalog_id)`
- `get_hatrac_store(hostname)` -- returns an authenticated `HatracStore` for object store
- `get_request_credential()` -- returns the credential dict for higher-level APIs that
  construct their own client:
  `DerivaML(hostname, catalog_id, credential=get_request_credential())`

Plugins never handle bearer token validation, introspection, or exchange. The credential
dict passed through `get_request_credential()` contains an opaque Credenza-derived token,
but the auth lifecycle is entirely managed by the core.

### Configuration

`pydantic-settings` `BaseSettings` with `env_prefix="DERIVA_MCP_"`. Supports environment
variables and optional `.env` file override.

| Variable                                | Required | Default | Description                                      |
|-----------------------------------------|----------|---------|--------------------------------------------------|
| `DERIVA_MCP_CREDENZA_URL`               | Yes      | --      | Credenza base URL                                |
| `DERIVA_MCP_SERVER_RESOURCE`            | Yes      | --      | Resource identifier for this MCP server          |
| `DERIVA_MCP_DERIVA_RESOURCE`            | Yes      | --      | Resource identifier to exchange to (DERIVA REST) |
| `DERIVA_MCP_CLIENT_ID`                  | Yes      | --      | This server's client ID (confidential client)    |
| `DERIVA_MCP_CLIENT_SECRET`              | Yes      | --      | This server's client secret                      |
| `DERIVA_MCP_TOKEN_CACHE_BUFFER_SECONDS` | No       | 60      | Near-expiry buffer for derived token cache       |

Resource identifiers may be HTTPS URIs or URNs; Credenza accepts both.

---

## Package Structure

```
deriva-mcp-core/
├── pyproject.toml
├── docs/
└── src/
    └── deriva_mcp_core/
        ├── __init__.py          # Public API: get_deriva_server(), get_hatrac_store(), get_request_credential()
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
        │   ├── api.py           # PluginContext class
        │   └── loader.py        # Entry point discovery and registration
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
- Validation: required fields raise on startup if missing

#### 1.2 Per-Request Context

- `context.py`: `_current_credential: ContextVar[dict | None]`
- `set_current_credential(cred: dict)` -- called by auth verifier or startup (internal)
- `get_request_credential() -> dict` -- public; raises if called outside handler context
- `get_deriva_server(hostname) -> DerivaServer` -- public; reads credential, constructs DerivaServer
- `get_hatrac_store(hostname) -> HatracStore` -- public; reads credential, constructs HatracStore

#### 1.3 Public API

Three functions exported from `deriva_mcp_core`:

- `get_deriva_server(hostname) -> DerivaServer` -- authenticated connection root for ERMREST.
  Tools call `.connect_ermrest(catalog_id)` on the returned server to get an ErmrestCatalog.
  Also provides `.connect_ermrest_alias()`, `.create_ermrest_catalog()`, etc.

- `get_hatrac_store(hostname) -> HatracStore` -- authenticated connection root for Hatrac object
  store operations. Separate from DerivaServer (distinct URL base, /hatrac/).

- `get_request_credential() -> dict` -- returns the per-request credential dict. Use when
  passing to a higher-level API that constructs its own client, e.g.:
  `DerivaML(hostname, catalog_id, credential=get_request_credential())`. Named distinctly
  from `deriva.core.get_credential()` (which reads from local disk) to avoid confusion for
  developers familiar with deriva-py.

Deliverable: `get_deriva_server()`, `get_hatrac_store()`, and `get_request_credential()` work with a
manually set credential. Unit tested.

---

### Phase 2 -- Auth Layer [TODO]

**Goal:** Credenza introspection, token exchange, and smart cache implemented and tested.
No server wiring yet.

#### 2.1 Introspection Client

- `auth/introspect.py`: async `introspect(token: str) -> IntrospectionResult`
- `IntrospectionResult`: dataclass with `active`, `sub`, `aud`, `exp`, raw response
- Uses `httpx.AsyncClient` with client credentials (`CLIENT_ID`, `CLIENT_SECRET`)
- Raises on inactive token or missing `sub`

#### 2.2 Token Exchange Client

- `auth/exchange.py`: async `exchange(subject_token: str, target_resource: str) -> ExchangeResult`
- `ExchangeResult`: dataclass with `access_token`, `expires_in`, computed `expires_at`
- RFC 8693 POST to Credenza `/token`

#### 2.3 Smart Token Cache

- `auth/token_cache.py`: `DerivedTokenCache`
- `async get(sub: str, subject_token: str) -> str`
    - Cache hit + not near expiry: return immediately
    - Miss or near expiry: acquire per-`sub` asyncio.Lock, recheck, exchange, store, return
- `invalidate(sub: str)` -- for explicit eviction (e.g., on 401 from downstream)
- Cache entries: `{sub: CacheEntry(derived_token, expires_at)}`

Deliverable: Auth layer fully unit tested with `pytest-httpx` mocks of Credenza endpoints.
No live Credenza dependency required for tests.

---

### Phase 3 -- Server Skeleton and Token Verifier [TODO]

**Goal:** A running FastMCP server with auth wiring. No tools yet, but the full
auth pipeline is end-to-end functional.

#### 3.1 Token Verifier

`mcp` 1.9+ provides a `TokenVerifier` protocol and a `token_verifier` parameter on
`FastMCP.__init__`. We implement this protocol rather than hand-rolling Starlette middleware.

- `auth/verifier.py`: `CredenzaTokenVerifier` implementing `mcp.server.auth.provider.TokenVerifier`
    - `async verify_token(token: str) -> AccessToken | None`
    - Calls `introspect(token)` -> validates `active` and `aud` contains `DERIVA_MCP_SERVER_RESOURCE`
    - Calls `token_cache.get(sub, token)` -> derived token
    - Calls `set_current_credential({"bearer-token": derived_token})`
    - Returns `AccessToken(token=derived_token, client_id=sub, scopes=..., expires_at=..., resource=...)`
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

- `plugin/api.py`: `PluginContext` -- wraps `FastMCP` instance, exposes `tool()`,
  `resource()`, `prompt()` decorators
- `plugin/loader.py`: `load_plugins(ctx: PluginContext)` -- discovers `deriva_mcp.plugins`
  entry points, calls each `register(ctx)`

Deliverable: Server starts, unauthenticated requests are rejected by FastMCP's auth layer,
`/health` responds without auth. Integration test with mocked Credenza confirms auth
pipeline end-to-end.

---

### Phase 4 -- Core ERMREST Tools [TODO]

**Goal:** Built-in tools covering the ERMREST primitive surface.

Each tool module has a `register(ctx: PluginContext)` function (same pattern as external
plugins). Registered directly in `server.py` before plugin discovery.

#### 4.1 Schema Introspection (`tools/catalog.py`)

- `get_catalog_info(hostname, catalog_id)` -- catalog metadata
- `list_schemas(hostname, catalog_id)` -- schema names
- `get_schema(hostname, catalog_id, schema)` -- tables, columns, keys, FKs for a schema
- `get_table(hostname, catalog_id, schema, table)` -- full table definition

#### 4.2 Entity CRUD (`tools/entity.py`)

- `get_entities(hostname, catalog_id, schema, table, filters?)` -- entity retrieval
- `insert_entities(hostname, catalog_id, schema, table, entities)` -- POST
- `update_entities(hostname, catalog_id, schema, table, entities)` -- PUT
- `delete_entities(hostname, catalog_id, schema, table, filters)` -- DELETE

#### 4.3 Queries (`tools/query.py`)

- `query_attribute(hostname, catalog_id, path, attributes?)` -- attribute query
- `query_aggregate(hostname, catalog_id, path, aggregates)` -- aggregate query

#### 4.4 Hatrac Object Store (`tools/hatrac.py`)

- `list_namespace(hostname, path)` -- list objects in namespace
- `get_object_metadata(hostname, path)` -- object metadata (not content)
- `create_namespace(hostname, path)` -- create namespace

Deliverable: All built-in tools implemented and unit tested. Server runs with full tool
surface via `--transport streamable-http`.

---

### Phase 5 -- Integration and Validation [TODO]

**Goal:** End-to-end validation against live Credenza + DERIVA.

#### 5.1 Integration Tests

- Test `/authorize` -> `/token` -> MCP bearer token flow against Credenza
- Test token verifier rejects expired/invalid tokens
- Test token cache hit/miss/near-expiry behavior under concurrent requests
- Test schema introspection tools against a real DERIVA catalog

#### 5.2 Plugin Smoke Test

- Write a minimal `test_plugin` (in the test suite, not a separate package) that uses
  `PluginContext` to register a custom tool
- Verify the tool is reachable and executes correctly with proper auth context

#### 5.3 Docker Compose Configuration

- `docker-compose.yml` for running `deriva-mcp-core` alongside Credenza
- Document required env vars
- Confirm `/health` suitable for Docker health probes

Deliverable: Full integration test suite passing. Reference compose config documented.

---

### Phase 6 -- Documentation and Handoff [TODO]

- README: installation, transport modes, configuration reference, plugin authoring guide
- Plugin authoring guide: how to write a `register(ctx)` function, package entry points,
  when to use `get_deriva_server()` / `get_hatrac_store()` vs `get_request_credential()`, testing patterns
- Deployment guide: VM, Docker Compose, Kubernetes patterns
- Note: deriva-ml tool port is out of scope; the plugin framework is the handoff artifact

---

## Out of Scope

- Backward compatibility with `deriva-mcp` prototype
- Port of deriva-ml tools (separate effort, handed to deriva-ml developer)
- Refresh token handling (Credenza derived sessions are fixed-lifetime, 30 min cap)
- Dynamic client registration
- JWKS / JWT token validation (Credenza issues opaque tokens only)