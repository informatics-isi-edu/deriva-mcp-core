# deriva-mcp-core

[![CI Status](https://github.com/informatics-isi-edu/deriva-mcp-core/actions/workflows/ci.yaml/badge.svg)](https://github.com/informatics-isi-edu/deriva-mcp-core/actions/workflows/ci.yaml)
[![Coverage Status](https://coveralls.io/repos/github/informatics-isi-edu/deriva-mcp-core/badge.svg?branch=main)](https://coveralls.io/github/informatics-isi-edu/deriva-mcp-core?branch=main)

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for the
[DERIVA](https://deriva.isi.edu/) platform. Exposes ERMrest and Hatrac as MCP tools
with per-request OAuth authentication via [Credenza](https://github.com/informatics-isi-edu/credenza).

## Contents

- [Transport Modes](#transport-modes)
- [Installation](#installation)
- [Quick Start](#quick-start)
    - [stdio (local development)](#stdio-local-development)
    - [HTTP (production)](#http-production)
- [Configuration Reference](#configuration-reference)
- [Built-in Tools](#built-in-tools)
- [Safety Controls](#safety-controls)
    - [Anonymous Access](#anonymous-access)
- [Plugin Framework](#plugin-framework)
- [Health Endpoint](#health-endpoint)

---

## Transport Modes

| Mode    | Auth                                                        | Use Case                          |
|---------|-------------------------------------------------------------|-----------------------------------|
| `stdio` | Local credential from `~/.deriva/credential.json`           | Local development, Claude Desktop |
| `http`  | Per-request Bearer token via Credenza introspect + exchange | Production multi-user deployment  |

---

## Installation

Requires Python 3.11+. Install with [uv](https://github.com/astral-sh/uv) (recommended) or pip.

```bash
# Install from the repository
uv pip install git+https://github.com/informatics-isi-edu/deriva-mcp-core.git

# With RAG support (ChromaDB and pgvector dependencies)
uv pip install "deriva-mcp-core[rag] @ git+https://github.com/informatics-isi-edu/deriva-mcp-core.git"
```

For development:

```bash
git clone https://github.com/informatics-isi-edu/deriva-mcp-core.git
cd deriva-mcp-core
uv sync --extra dev --extra rag
```

---

## Quick Start

### stdio (local development)

stdio mode reads credentials from `~/.deriva/credential.json` -- the same file used
by `deriva-py` CLI tools. No additional configuration is required.

Run the server:

```bash
deriva-mcp-core
# or explicitly:
deriva-mcp-core --transport stdio
```

Configure Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "deriva": {
      "command": "deriva-mcp-core",
      "args": [
        "--transport",
        "stdio"
      ]
    }
  }
}
```

By default, all mutating tools are disabled in stdio mode as well -- set
`DERIVA_MCP_DISABLE_MUTATING_TOOLS=false` if you need write access.

### HTTP (production)

HTTP mode validates each request's Bearer token against Credenza and exchanges it for a
DERIVA-scoped derived token. Create a `deriva-mcp.env` file (see
[Configuration Reference](#configuration-reference)) and run:

```bash
deriva-mcp-core --transport http --host 0.0.0.0 --port 8000
```

The server loads `deriva-mcp.env` from the first path found in:

1. `/etc/deriva-mcp/deriva-mcp.env` (system-wide)
2. `~/deriva-mcp.env` (user home)
3. `./deriva-mcp.env` (current directory)

Use `--config /path/to/file` to override.

Minimal `deriva-mcp.env` for HTTP:

```ini
DERIVA_MCP_CREDENZA_URL = https://your-host/authn
DERIVA_MCP_SERVER_URL = https://your-host/mcp
DERIVA_MCP_SERVER_RESOURCE = https://your-host/mcp
DERIVA_MCP_CLIENT_SECRET = your-client-secret
DERIVA_MCP_DISABLE_MUTATING_TOOLS = false
```

For Docker Compose deployments, see the
[deriva-docker](https://github.com/informatics-isi-edu/deriva-docker) repository
and the [Deployment Guide](docs/deployment-guide.md).

---

## Configuration Reference

### Core settings

| Variable                                  | Default                       | Description                                                                                                                                |
|-------------------------------------------|-------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| `DERIVA_MCP_CREDENZA_URL`                 | *(required for HTTP)*         | Base URL of the Credenza instance                                                                                                          |
| `DERIVA_MCP_SERVER_URL`                   | *(required for HTTP)*         | Public HTTPS URL of this MCP server                                                                                                        |
| `DERIVA_MCP_SERVER_RESOURCE`              | *(required for HTTP)*         | Resource identifier for this server (usually same as SERVER_URL)                                                                           |
| `DERIVA_MCP_CLIENT_SECRET`                | *(required for HTTP)*         | Client secret for Credenza token exchange                                                                                                  |
| `DERIVA_MCP_CLIENT_ID`                    | `deriva-mcp`                  | Client ID registered with Credenza                                                                                                         |
| `DERIVA_MCP_DERIVA_RESOURCE`              | `urn:deriva:rest:service:all` | Resource identifier to request in token exchange                                                                                           |
| `DERIVA_MCP_ALLOW_ANONYMOUS`              | `false`                       | Allow unauthenticated requests (see [Anonymous Access](#anonymous-access))                                                                 |
| `DERIVA_MCP_DISABLE_MUTATING_TOOLS`       | `true`                        | When `true`, all tools registered as mutating return an error without executing                                                            |
| `DERIVA_MCP_PLUGIN_ALLOWLIST`             | *(unset -- allow all)*        | Comma-separated list of plugin entry point names to load; empty string disables all external plugins                                       |
| `DERIVA_MCP_MUTATION_REQUIRED_CLAIM`      | *(unset)*                     | JSON claim spec that must match the token introspection payload before mutations are permitted (e.g. `{"groups": ["deriva-mcp-mutator"]}`) |
| `DERIVA_MCP_ADMIN_REQUIRED_CLAIM`         | *(unset)*                     | JSON claim spec required for admin-only tools. **Fails closed when unset**, unlike the mutation claim (see below)                          |
| `DERIVA_MCP_TOKEN_CACHE_BUFFER_SECONDS`   | `60`                          | Re-exchange derived tokens this many seconds before they expire                                                                            |
| `DERIVA_MCP_INTROSPECT_CACHE_TTL_SECONDS` | `60`                          | How long to cache token introspection results                                                                                              |
| `DERIVA_MCP_AUDIT_LOGFILE_PATH`           | `deriva-mcp-audit.log`        | Audit log file path (used when syslog is off)                                                                                              |
| `DERIVA_MCP_AUDIT_USE_SYSLOG`             | `false`                       | Write audit events to syslog (`/dev/log`) instead of a file                                                                                |
| `DERIVA_MCP_HOSTNAME_MAP`                 | `{}`                          | JSON object mapping external hostnames to internal aliases (e.g. `{"localhost":"deriva"}`)                                                 |
| `DERIVA_MCP_SSL_VERIFY`                   | `true`                        | TLS verification for outbound calls: `true`, `false`, or path to a CA bundle                                                               |
| `DERIVA_MCP_DEBUG`                        | `false`                       | Enable DEBUG-level logging                                                                                                                 |

### RAG settings

| Variable                        | Default                   | Description                                                     |
|---------------------------------|---------------------------|-----------------------------------------------------------------|
| `DERIVA_MCP_RAG_ENABLED`        | `false`                   | Enable the RAG subsystem                                        |
| `DERIVA_MCP_RAG_VECTOR_BACKEND` | `chroma`                  | Vector store backend: `chroma` or `pgvector`                    |
| `DERIVA_MCP_RAG_CHROMA_DIR`     | `~/.deriva-mcp/chroma`    | Persistence directory for embedded ChromaDB                     |
| `DERIVA_MCP_RAG_CHROMA_URL`     | *(unset)*                 | ChromaDB server URL (enables server mode instead of embedded)   |
| `DERIVA_MCP_RAG_PG_DSN`         | *(required for pgvector)* | PostgreSQL DSN for pgvector backend                             |
| `DERIVA_MCP_RAG_AUTO_UPDATE`    | `true`                    | Crawl and incrementally update documentation sources on startup |
| `DERIVA_MCP_RAG_DATA_DIR`       | `~/.deriva-mcp/rag`       | SHA cache and runtime-added sources file                        |

---

## Built-in Tools

Schema introspection, entity CRUD, queries, Hatrac, catalog administration,
annotations, schema DDL, vocabulary, and background-task tools are documented in
the [Usage Guide](docs/usage-guide.md). Mutating tools among them are disabled by
default (`DERIVA_MCP_DISABLE_MUTATING_TOOLS=true`); set it to `false` to enable them.

The RAG tools are listed here because their admin gating is configured separately
from the mutation kill switch:

### RAG tools

| Tool                    | Description                                                    | Admin? |
|-------------------------|----------------------------------------------------------------|--------|
| `rag_search`            | Semantic search across documentation and catalog schemas       |        |
| `rag_status`            | Per-source chunk counts, timestamps, and indexed schema hashes |        |
| `rag_update_docs`       | Incremental documentation update (SHA delta, runs inline)      | Yes    |
| `rag_update_docs_async` | Same as above, submitted as a background task                  | Yes    |
| `rag_index_schema`      | Manually trigger schema reindex for a catalog                  |        |
| `rag_index_table`       | Index rows from a specific table into the vector store         |        |
| `rag_ingest`            | Force a full re-crawl of one or all documentation sources      | Yes    |
| `rag_ingest_datasets`   | Force re-enrichment of registered dataset indexers             | Yes    |
| `rag_add_source`        | Register a new documentation source at runtime (persisted)     | Yes    |
| `rag_remove_source`     | Remove a runtime-added documentation source                    | Yes    |
| `rag_import_chunks`     | Bulk-import pre-built chunks from a local JSON file            | Yes    |

"Admin?" tools require `DERIVA_MCP_ADMIN_REQUIRED_CLAIM` -- see
[Admin-only tool claim gating](#admin-only-tool-claim-gating).

---

## Safety Controls

### Mutation kill switch

All tools that write to the DERIVA catalog are registered with `mutates=True`. When
`DERIVA_MCP_DISABLE_MUTATING_TOOLS=true` (the default), these tools return an error
immediately without executing. The server logs a warning on startup when mutations are
disabled and an info message when they are enabled, so the active state is always visible
in the logs.

RAG tools write to the local vector store, not the DERIVA catalog, and are not affected
by this setting. The subset of RAG tools that affect shared/global server state (full
re-crawls, source registration, bulk chunk import) are instead gated by the separate
admin claim below.

### Plugin allowlist

By default all installed plugins (packages that declare the `deriva_mcp.plugins` entry
point) are loaded. To restrict loading to a known set:

```ini
# Load only the deriva-ml plugin
DERIVA_MCP_PLUGIN_ALLOWLIST = deriva-ml

# Disable all external plugins
DERIVA_MCP_PLUGIN_ALLOWLIST =
```

Blocked plugins are logged at WARNING level so operators can audit what was skipped.

### Anonymous access

Set `DERIVA_MCP_ALLOW_ANONYMOUS=true` to allow requests without an `Authorization`
header. This is useful for deployments serving publicly readable catalogs where
requiring users to authenticate would be unnecessary friction.

**Behavior:**

- **No token provided:** the request proceeds with empty DERIVA credentials, equivalent
  to what an unauthenticated browser request would receive. Mutations are blocked
  regardless of the kill-switch setting -- anonymous access is always read-only.
- **Valid token provided:** normal Credenza validation and token exchange; the request
  runs as that authenticated user with their normal permissions.
- **Invalid/expired token provided:** 401 response. A client that sends a token must
  send a valid one -- bad tokens are never silently downgraded to anonymous.

**Sub-modes:**

| `DERIVA_MCP_CREDENZA_URL` set? | Mode           | Effect                                                                      |
|--------------------------------|----------------|-----------------------------------------------------------------------------|
| Yes                            | Mixed          | Both authenticated and anonymous requests work                              |
| No                             | Anonymous-only | Credenza fields are not required at startup; any provided token is rejected |

Example (anonymous-only, public read-only catalog):

```ini
DERIVA_MCP_ALLOW_ANONYMOUS = true
# No DERIVA_MCP_CREDENZA_URL, SERVER_URL, CLIENT_SECRET etc. needed
```

Example (mixed mode -- auth optional):

```ini
DERIVA_MCP_ALLOW_ANONYMOUS = true
DERIVA_MCP_CREDENZA_URL = https://your-host/authn
DERIVA_MCP_SERVER_URL = https://your-host/mcp
DERIVA_MCP_SERVER_RESOURCE = https://your-host/mcp
DERIVA_MCP_CLIENT_ID = deriva-mcp
DERIVA_MCP_CLIENT_SECRET = your-client-secret
```

### Per-user mutation claim gating

When the mutation kill switch is off, all authenticated users can execute mutating tools.
To restrict mutations to users with a specific claim in their token introspection payload:

```ini
# Require membership in a specific group
DERIVA_MCP_MUTATION_REQUIRED_CLAIM = {"groups": ["deriva-mcp-mutator"]}

# Require a boolean claim
DERIVA_MCP_MUTATION_REQUIRED_CLAIM = {"mcp_can_mutate": true}
```

List values use OR semantics (any match is sufficient). Multiple keys use AND semantics
(all must match). Users who fail the check receive an error response and a
`mutation_claim_denied` audit event. The kill switch takes precedence when both
controls are active.

### Admin-only tool claim gating

Some tools are registered with `admin=True` instead of (or alongside) `mutates=True` --
tools that affect shared/global server state rather than the caller's own scoped data.
Currently this covers the RAG tools that trigger full re-crawls or persist/remove
documentation sources: `rag_ingest`, `rag_ingest_datasets`, `rag_update_docs`,
`rag_update_docs_async`, `rag_add_source`, `rag_remove_source`, `rag_import_chunks`.
`rag_search`, `rag_status`, `rag_index_schema`, and `rag_index_table` are not gated --
they only touch data the caller can already see via ERMrest.

```ini
DERIVA_MCP_ADMIN_REQUIRED_CLAIM = {"groups": ["deriva-mcp-admin"]}
```

Same `claim_spec` format as `DERIVA_MCP_MUTATION_REQUIRED_CLAIM` (List values use OR
semantics; multiple keys use AND semantics). **Unlike the mutation claim, this fails
closed**: if `DERIVA_MCP_ADMIN_REQUIRED_CLAIM` is unset, every HTTP-mode caller is denied
admin-only tools, not permitted. There is no kill-switch equivalent -- set the claim to
grant access, there is nothing to flip to allow it without one. Stdio mode (single local
user, no token) always permits admin-only tools regardless of this setting. Users who
fail the check receive an error response and an `admin_claim_denied` audit event.

---

## Plugin Framework

External packages can register additional MCP tools, resources, and prompts by declaring
a `register` entry point in the `deriva_mcp.plugins` group:

```toml
# pyproject.toml of the plugin package
[project.entry-points."deriva_mcp.plugins"]
my-plugin = "my_package.mcp_plugin:register"
```

The `register` function receives a `PluginContext` that exposes the same API used by the
built-in tool modules:

```python
from deriva_mcp_core import get_catalog, get_hatrac_store
from deriva_mcp_core.context import deriva_call


def register(ctx):
    @ctx.tool(mutates=False)
    async def my_read_tool(hostname: str, catalog_id: str) -> str:
        with deriva_call():
            catalog = get_catalog(hostname, catalog_id)
            # ... ERMrest calls here
        return result

    @ctx.tool(mutates=True)
    async def my_write_tool(hostname: str, catalog_id: str, data: dict) -> str:
        with deriva_call():
            catalog = get_catalog(hostname, catalog_id)
            # ... mutating calls here
        return result
```

Key rules:

- Every `ctx.tool()` call requires an explicit `mutates=True` or `mutates=False` (omitting raises `TypeError` at
  startup).
- Wrap all ERMrest/Hatrac calls in `with deriva_call():` for transparent 401 handling.
- Mutating tools (`mutates=True`) emit `audit_event` on success and failure.
- Use `ctx.submit_task(coroutine, name)` for long-running operations; returns a `task_id` immediately.

See [docs/plugin-authoring-guide.md](docs/plugin-authoring-guide.md) for the full guide covering
credential access patterns, lifecycle hooks, RAG extension, and testing.

---

## Further Reading

- [Usage Guide](docs/usage-guide.md) -- natural language prompt examples for all tool categories
- [Plugin Authoring Guide](docs/plugin-authoring-guide.md) -- writing and testing external plugins
- [Deployment Guide](docs/deployment-guide.md) -- Docker Compose, VM, reverse proxy, vector store backends
- [ADR-0001: Async Architecture](docs/ADR-0001-async-architecture.md) -- why ASGI/uvicorn is non-negotiable for MCP services
- [ADR-0002: Plugin-Contributed System Prompt Extensions](docs/ADR-0002-system-prompt-extensions.md) -- proposed framework for plugins to extend the chatbot's LLM system prompt (Proposed, not yet implemented)

---

## Health Endpoint

The HTTP server exposes a health endpoint at `GET /health` that returns `{"status": "ok"}`
with no authentication required. Suitable for Docker health probes and load balancer checks.

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

---

## Development Status

`deriva-mcp-core` is alpha-quality software. The API and configuration surface are
still evolving and breaking changes may occur between releases without advance notice.
It has been validated end-to-end against live DERIVA deployments, but has not yet seen
broad production use. Use in production environments is at your own risk. Bug reports
and contributions are welcome via the
[issue tracker](https://github.com/informatics-isi-edu/deriva-mcp-core/issues).