  ---
  # deriva-mcp-core

  A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server reference implementation
  for the [DERIVA](https://deriva-web.readthedocs.io/) platform.

  ## Overview

  `deriva-mcp-core` provides MCP tool access to DERIVA REST services (ERMRest and Hatrac) with
  proper per-request OAuth authentication via [Credenza](https://github.com/informatics-isi-edu/credenza).

  Core capabilities:

  - Schema introspection (catalogs, schemas, tables, columns, keys, foreign keys)
  - Entity CRUD (get, insert, update, delete)
  - Attribute and aggregate queries
  - Hatrac object store operations

  Authentication is handled transparently: incoming bearer tokens are validated via [Credenza](https://github.com/informatics-isi-edu/credenza)
  token introspection, exchanged for DERIVA-scoped derived tokens, and cached per user for
  the lifetime of the derived session.

  ## Extensibility

  `deriva-mcp-core` includes a plugin framework for registering additional tool sets without
  modifying the core package. Higher-level libraries (e.g., `deriva-ml`) can extend the server
  by declaring a `register(ctx)` entry point.

  ## Transport Modes

  | Mode | Auth | Use Case |
  |------|------|----------|
  | `streamable-http` | Per-request OAuth (Credenza introspect + exchange) | Production |
  | `stdio` | Local credential from disk (`~/.deriva/credential.json`) | Local development |

  ## Documentation

  - [Implementation Workplan](docs/workplan-deriva-mcp-core.md)

  ## Status

  Early development. See workplan for phase status.

  ---
