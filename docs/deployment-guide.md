# Deployment Guide

## Contents

- [Overview](#overview)
- [Docker Compose (deriva-docker)](#docker-compose-deriva-docker)
- [Standalone Docker](#standalone-docker)
- [VM / Bare Metal](#vm--bare-metal)
    - [Reverse Proxy Configuration](#reverse-proxy-configuration)
    - [systemd Service](#systemd-service)
- [Vector Store Backend Selection](#vector-store-backend-selection)
- [Logging and Audit](#logging-and-audit)
- [TLS and CA Certificates](#tls-and-ca-certificates)
- [MCP Client Configuration](#mcp-client-configuration)

---

## Overview

`deriva-mcp-core` runs as an HTTP service on port 8000. In production it sits behind
a TLS-terminating reverse proxy (Traefik, Apache, or nginx) that:

1. Terminates TLS
2. Strips the `/mcp` path prefix before forwarding to the server
3. Routes `/.well-known/oauth-protected-resource/mcp` to the server without stripping
   (RFC 9728 protected resource metadata)

Authentication is handled entirely by the server itself -- no `ForwardAuth` middleware
or external auth proxy is needed. The server validates each Bearer token via Credenza
introspection and exchanges it for a DERIVA-scoped derived token.

The server must be registered as a confidential client in Credenza with:

- `authorization_code`, `token_exchange`, and `client_credentials` grant types
- The MCP server URL as the allowed resource

**Anonymous / zero-auth mode:** Set `DERIVA_MCP_ALLOW_ANONYMOUS=true` to allow
unauthenticated requests. Requests without an `Authorization` header are processed
with empty DERIVA credentials (equivalent to public/anonymous catalog access) and
with mutations blocked. A provided but invalid token is still rejected with 401.
When `DERIVA_MCP_CREDENZA_URL` is also set, authenticated and anonymous requests
are both supported (mixed mode). When `DERIVA_MCP_CREDENZA_URL` is not set,
all Credenza fields become optional and the server operates in anonymous-only mode.

---

## Docker Compose (deriva-docker)

The [deriva-docker](https://github.com/informatics-isi-edu/deriva-docker) repository
provides a production-ready Docker Compose stack that includes Traefik, Apache,
Credenza, Keycloak, and `deriva-mcp-core`. This is the recommended deployment path.

### Setup

Generate the environment file with the `--enable-mcp` flag:

```bash
cd deriva-docker
./utils/generate-env.sh --enable-mcp [other flags] --hostname your.host.example.org
```

This generates a `.env` file and creates the `mcp_client_secret.txt` Docker secret
file under `secrets/`. The Credenza entrypoint hashes the secret at startup using
argon2 and injects it into `client_registry.json`.

Start the stack with the `deriva-mcp` profile:

```bash
docker compose --profile deriva-mcp up -d
```

### Installing plugins

Plugins are installed at image build time via the `EXTRA_PACKAGES` build argument.
Each entry is passed directly to `uv pip install`:

```bash
# In the environment file or as a compose override
DERIVA_MCP_EXTRA_PACKAGES="my-plugin>=1.0 another-plugin@git+https://github.com/org/another.git"
```

Or set the build arg directly:

```bash
docker compose build --build-arg "EXTRA_PACKAGES=my-plugin>=1.0"
```

### Building from a specific branch

```bash
# .env or docker compose override
DERIVA_MCP_CORE_GIT_BRANCH=my-feature-branch
```

The default is `main`. Setting the environment variable overrides the Dockerfile
default. Setting it to an empty string falls back to `main` (the compose file uses
`${DERIVA_MCP_CORE_GIT_BRANCH:-main}`).

### Using a local source checkout

Place a `deriva-mcp-core` checkout under `deriva/mcp/src/deriva-mcp-core/`. If
`pyproject.toml` is present there, the build uses it instead of cloning from
GitHub. Set `GIT_FETCH=1` in the build args to force a fresh clone regardless.

---

## Standalone Docker

To run `deriva-mcp-core` without the full deriva-docker stack (e.g., for testing
against an existing Credenza + DERIVA deployment):

```bash
docker build \
  -t deriva-mcp-core \
  --build-arg DERIVA_MCP_CORE_GIT_BRANCH=main \
  deriva/mcp/

docker run -d \
  --name deriva-mcp \
  -p 8000:8000 \
  --env-file /path/to/deriva-mcp.env \
  -e DERIVA_MCP_CLIENT_SECRET="$(cat /run/secrets/mcp_client_secret)" \
  deriva-mcp-core
```

The server listens on `0.0.0.0:8000` by default. Place a TLS-terminating reverse
proxy in front of it for production use.

---

## VM / Bare Metal

### Installation

```bash
# Install into a dedicated virtualenv
python3.11 -m venv /opt/deriva-mcp
/opt/deriva-mcp/bin/pip install "deriva-mcp-core[rag] @ git+https://github.com/informatics-isi-edu/deriva-mcp-core.git"

# Install plugins
/opt/deriva-mcp/bin/pip install my-plugin>=1.0
```

### Configuration file

Create `/etc/deriva-mcp/deriva-mcp.env`:

```ini
DERIVA_MCP_CREDENZA_URL = https://your-host/authn
DERIVA_MCP_SERVER_URL = https://your-host/mcp
DERIVA_MCP_SERVER_RESOURCE = https://your-host/mcp
DERIVA_MCP_CLIENT_ID = deriva-mcp
DERIVA_MCP_CLIENT_SECRET = your-client-secret
DERIVA_MCP_DISABLE_MUTATING_TOOLS = false
DERIVA_MCP_AUDIT_USE_SYSLOG = true
DERIVA_MCP_RAG_ENABLED = true
```

Environment variables take precedence over the file, so secrets can be injected
by the process manager without touching the file.

### Reverse Proxy Configuration

The reverse proxy must:

1. Forward `/mcp` requests to `http://127.0.0.1:8000/` with the prefix stripped
2. Forward `/.well-known/oauth-protected-resource/mcp` to
   `http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp`
   **without** stripping (the well-known path must reach the server intact)

**Traefik labels** (already configured in deriva-docker):

```yaml
- "traefik.http.routers.mcp.rule=PathPrefix(`/mcp`)"
- "traefik.http.routers.mcp.middlewares=mcp-stripprefix"
- "traefik.http.middlewares.mcp-stripprefix.stripPrefix.prefixes=/mcp"
- "traefik.http.routers.mcp-wellknown.rule=Path(`/.well-known/oauth-protected-resource/mcp`)"
- "traefik.http.routers.mcp-wellknown.service=mcp"
# (no strip middleware on mcp-wellknown)
```

**Apache** (`/etc/apache2/sites-available/mcp.conf`):

```apache
ProxyPass        /mcp  http://127.0.0.1:8000
ProxyPassReverse /mcp  http://127.0.0.1:8000

# RFC 9728: well-known route, no prefix stripping
ProxyPass        /.well-known/oauth-protected-resource/mcp  http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp
ProxyPassReverse /.well-known/oauth-protected-resource/mcp  http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp
```

**nginx**:

```nginx
location /mcp/ {
    proxy_pass http://127.0.0.1:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

# RFC 9728: well-known route, no rewrite
location = /.well-known/oauth-protected-resource/mcp {
    proxy_pass http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp;
    proxy_set_header Host $host;
}
```

### systemd Service

`/etc/systemd/system/deriva-mcp.service`:

```ini
[Unit]
Description = deriva-mcp-core MCP server
After = network.target

[Service]
Type = simple
User = deriva-mcp
EnvironmentFile = /etc/deriva-mcp/deriva-mcp.env
ExecStart = /opt/deriva-mcp/bin/deriva-mcp-core --transport http --host 127.0.0.1 --port 8000
Restart = on-failure
RestartSec = 5
StandardOutput = journal
StandardError = journal

[Install]
WantedBy = multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now deriva-mcp
```

---

## Vector Store Backend Selection

### ChromaDB (default)

Zero additional services. Suitable for single-instance deployments or development.

```ini
DERIVA_MCP_RAG_ENABLED = true
DERIVA_MCP_RAG_VECTOR_BACKEND = chroma
DERIVA_MCP_RAG_CHROMA_DIR = /var/lib/deriva-mcp/chroma
```

The persistence directory must be writable by the server process and must survive
container restarts (mount as a volume in Docker).

**ChromaDB server mode** allows multiple instances to share one vector store:

```ini
DERIVA_MCP_RAG_CHROMA_URL = http://chroma:8000
```

When `CHROMA_URL` is set, `CHROMA_DIR` is ignored. The ChromaDB HTTP server must
be running and reachable from the MCP server.

### pgvector

Recommended for production multi-instance deployments. Requires a PostgreSQL
database with the `pgvector` extension installed.

```ini
DERIVA_MCP_RAG_ENABLED = true
DERIVA_MCP_RAG_VECTOR_BACKEND = pgvector
DERIVA_MCP_RAG_PG_DSN = postgresql://deriva_mcp:password@db:5432/deriva_mcp
```

The server creates the required table and index automatically on first startup --
no manual schema migration is needed. The database user must have `CREATE TABLE`
privilege in the target database.

**Enable pgvector on PostgreSQL:**

```sql
CREATE
EXTENSION IF NOT EXISTS vector;
```

**Choosing between backends:**

| Criteria             | ChromaDB (embedded)   | ChromaDB (server)       | pgvector              |
|----------------------|-----------------------|-------------------------|-----------------------|
| Additional services  | None                  | ChromaDB container      | PostgreSQL + pgvector |
| Multi-instance safe  | No (file locking)     | Yes                     | Yes                   |
| Existing PostgreSQL  | Not relevant          | Not relevant            | Reuse existing DB     |
| Operational overhead | Minimal               | Low                     | Moderate              |
| Recommended for      | Dev / single instance | Shared dev / small prod | Production            |

**Migration** between backends is not automated. To migrate, delete the old store
and allow the server to re-index on next startup. Documentation sources are
re-crawled from GitHub; catalog schema indexes are rebuilt on the next catalog
access. Data indexes (if any) must be rebuilt by the plugin that created them.

---

## Logging and Audit

The server emits two log streams:

**Application log** -- startup, request lifecycle, plugin loading, and token cache
events. Written to stderr (visible in `docker logs` or the systemd journal). Also
forwarded to syslog when `/dev/log` is available.

**Audit log** -- structured JSON records for authentication events and catalog
mutations. Written to `deriva-mcp-audit.log` by default, or to syslog when
`DERIVA_MCP_AUDIT_USE_SYSLOG=true`. In the deriva-docker stack, syslog is forwarded
to the centralized rsyslog collector and from there to Loki/Grafana.

Production deployments should set:

```ini
DERIVA_MCP_AUDIT_USE_SYSLOG = true
```

This routes audit events through rsyslog to the same central log collector as other
DERIVA services, so auth and mutation events appear alongside ERMrest and Apache logs.

### Audit event reference

| Event                                               | Trigger                                  |
|-----------------------------------------------------|------------------------------------------|
| `token_verified`                                    | Successful Bearer token validation       |
| `token_inactive`                                    | Token rejected (inactive or expired)     |
| `token_introspection_failed`                        | Credenza introspection call failed       |
| `token_audience_mismatch`                           | Token does not match server resource     |
| `token_exchange_success`                            | Derived token issued (cache miss)        |
| `token_exchange_failed`                             | Token exchange request failed            |
| `mutation_claim_denied`                             | Caller lacks the required mutation claim |
| `entity_insert` / `entity_insert_failed`            | Row insertion                            |
| `entity_update` / `entity_update_failed`            | Row update                               |
| `entity_delete` / `entity_delete_failed`            | Row deletion                             |
| `hatrac_create_namespace` / `_failed`               | Hatrac namespace creation                |
| `catalog_create` / `_failed`                        | Catalog creation                         |
| `catalog_delete` / `_failed`                        | Catalog deletion                         |
| `catalog_clone_async_submitted`                     | Async catalog clone submitted            |
| `task_completed` / `task_failed` / `task_cancelled` | Background task state transition         |
| `annotation_*`                                      | Annotation write operations              |

Plugin-defined mutating tools add their own events following the same naming
convention (`<plugin>_<operation>` / `<plugin>_<operation>_failed`).

---

## TLS and CA Certificates

The server makes outbound HTTPS connections to Credenza and DERIVA. In production
these should be signed by a public CA and `DERIVA_MCP_SSL_VERIFY=true` (the default)
is correct.

For dev/staging deployments with a private CA:

```ini
# Option 1: point at a CA bundle file
DERIVA_MCP_SSL_VERIFY = /etc/ssl/certs/my-ca.crt

# Option 2: install into the system CA store and leave DERIVA_MCP_SSL_VERIFY=true
```

In the deriva-docker stack the entrypoint installs the CA certificate from
`/certs-ext/$CA_FILENAME` into the system store and runs `update-ca-certificates`
before starting the server. Mount the `deriva/certs/` directory as `/certs-ext:ro`
and set `CA_FILENAME` in the container environment.

---

## MCP Client Configuration

Credenza does not support dynamic client registration (RFC 7591). Every MCP client
must be pre-registered in `client_registry.json` with its `client_id`. The default
client ID is `deriva-mcp` (matching `DERIVA_MCP_CLIENT_ID`).

### Claude Desktop

> **Note:** Claude Desktop cannot connect to MCP servers running on `localhost` due
> to a current restriction in the application. Use Claude Code for local development
> and testing against a local DERIVA stack.

Add the server using the Claude Desktop UI: **Settings > Integrations > Add MCP
Server**. Enter the server URL (`https://your-host/mcp`), then click **Advanced**
and set the OAuth Client ID to the value registered in `client_registry.json`
(default: `deriva-mcp`).

Claude Desktop discovers the OAuth endpoints automatically via RFC 8414 metadata at
`https://your-host/.well-known/oauth-authorization-server/authn` and RFC 9728
protected resource metadata at
`https://your-host/.well-known/oauth-protected-resource/mcp`.

The redirect URI used by Claude Desktop (`https://claude.ai/api/mcp/auth_callback`)
must be listed in `allowed_redirect_uris` for the client entry in
`client_registry.json`.

### Claude Code

```bash
claude mcp add --transport http \
  --url https://your-host/mcp \
  --client-id deriva-mcp \
  --callback-port 8080 \
  deriva-mcp
```

`--client-id` must match the entry in `client_registry.json`. `--callback-port`
determines the local redirect URI (`http://localhost:<port>/callback`); that URI
must be listed in `allowed_redirect_uris` for the client.

### VS Code

VS Code's MCP support (via GitHub Copilot Chat) does not support the OAuth flow
required for HTTP transport. Use stdio transport instead, configured in
`settings.json` (see the
[VS Code MCP configuration reference](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration#_configuration-file)):

```json
{
  "mcp": {
    "servers": {
      "deriva": {
        "type": "stdio",
        "command": "deriva-mcp-core",
        "args": [
          "--transport",
          "stdio"
        ],
        "env": {
          "DERIVA_MCP_DISABLE_MUTATING_TOOLS": "false"
        },
        "envFile": "${workspaceFolder}/.deriva-mcp.env"
      }
    }
  }
}
```

`env` sets individual variables; `envFile` loads a full env file (useful for
keeping server settings out of `settings.json`). Both are optional -- with neither,
the server starts with defaults and reads credentials from
`~/.deriva/credential.json`. No Credenza registration is required for stdio mode.

### Other MCP clients

Any RFC 9728-compliant MCP client can discover the server's OAuth requirements
automatically by fetching:

```
GET https://your-host/.well-known/oauth-protected-resource/mcp
```

The response includes the authorization server URL, supported scopes, and resource
identifier. The client must supply the pre-registered `client_id` and a redirect URI
listed in `allowed_redirect_uris` when initiating the OAuth flow -- dynamic client
registration is not supported.