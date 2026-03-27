# deriva-mcp-ui Workplan and Design

**Status:** Design -- not yet started

**Target repo:** `deriva-mcp-ui` (currently living in `deriva-mcp-core/docs/` until the repo is created)

---

## Overview

`deriva-mcp-ui` is a browser-based chatbot interface for DERIVA. It wraps the Claude LLM
and the `deriva-mcp-core` MCP server behind a web frontend, giving end users a natural
language interface to query and manage DERIVA catalogs without needing a desktop MCP client
(Claude Desktop, Claude Code, etc.).

Design goals:

- Standard web login via Credenza (no token pasting, no special client setup)
- Two operating modes: **default-catalog** (anchored to a specific catalog, for production
  deployments) and **general-purpose** (user specifies catalog, for platform-level access)
- Streaming responses via SSE -- Claude's replies appear incrementally
- Clean separation from `deriva-mcp-core` -- the UI service is an MCP client over HTTP;
  the MCP server is unchanged
- Deployable in the `deriva-docker` compose stack or behind Apache in a VM deployment

---

## Architecture

```
Browser (HTML + JS)
  |
  | HTTPS (session cookie)
  v
deriva-mcp-ui  (FastAPI/Starlette)
  |                    |
  | MCP HTTP           | HTTPS
  | (bearer token)     | (Claude API)
  v                    v
deriva-mcp-core     Anthropic API
  |
  | HTTPS
  v
DERIVA (ERMrest, Hatrac)
  |
  v
Credenza (OAuth AS -- introspect + exchange, as today)
```

### What the UI service does

1. Serves the browser chat UI (static HTML/JS)
2. Handles Credenza OAuth login and manages server-side user sessions
3. On each chat request: connects to the MCP server as an MCP client using the user's
   bearer token, runs the Claude tool-calling loop, streams text chunks back to the browser
   via SSE

### What the UI service does NOT do

- Execute DERIVA tools directly -- all tool execution happens inside `deriva-mcp-core`
- Validate or exchange tokens itself -- the MCP server's auth layer handles that
- Maintain a persistent MCP connection -- each chat turn opens a fresh stateless-HTTP
  MCP request (matching the MCP server's `stateless_http=True` model)

---

## Token Chain

The token chain has three layers. Understanding which layer controls which lifetime is
important for configuring the deployment correctly.

```
Credenza issues user bearer token
  (lifetime: operator-configured on the UI client registration -- e.g. 8h, 16h, 24h)
      |
      | UI service presents this token to the MCP server
      v
MCP server introspects via Credenza, exchanges for a derived DERIVA token
  (lifetime: 30-min hard cap on SessionType.DERIVED -- managed by DerivedTokenCache)
      |
      | Derived token used for all ERMrest / Hatrac calls within the request
      v
DERIVA
```

The 30-minute derived token cap is transparent to the UI layer. `DerivedTokenCache` in
`deriva-mcp-core` re-exchanges automatically on every cache miss as long as the upstream
bearer token is still valid. The UI service only ever holds and presents the user's
long-lived bearer token.

The only session lifecycle event the UI service must handle is the user's Credenza session
expiry (at whatever TTL is configured on the UI client registration). When the bearer token
expires, the MCP server returns 401, and the UI service redirects the browser to re-authenticate.

---

## Auth Flow

The UI service acts as a Credenza Relying Party (same model as Chaise) using the
authorization_code flow with PKCE.

```
1. Browser hits /
2. No valid session -> redirect to Credenza GET /authorize
      client_id=deriva-mcp-ui
      response_type=code
      resource=<DERIVA_MCP_SERVER_RESOURCE>   <-- request token scoped for MCP server
      redirect_uri=<DERIVA_CHATBOT_PUBLIC_URL>/callback
      code_challenge=<PKCE challenge>
      state=<CSRF token>

3. User authenticates at Credenza (Keycloak, Globus, etc.)
4. Credenza redirects to /callback?code=...&state=...
5. UI service: verify state, POST /token (authorization_code grant) with PKCE verifier
6. Credenza returns bearer token scoped to DERIVA_MCP_SERVER_RESOURCE
7. UI service stores token in server-side session (keyed by secure session cookie)
8. Browser is redirected to the chat UI

On each chat request:
9. UI service reads bearer token from session
10. Connects to MCP server with Authorization: Bearer <token>
11. MCP server validates via Credenza introspect + exchange as normal
```

By requesting `resource=<DERIVA_MCP_SERVER_RESOURCE>` in step 2, the token Credenza
issues is already scoped for the MCP server. No second token exchange step is needed at
the UI layer.

### Mutation access

The `DERIVA_MCP_MUTATION_REQUIRED_CLAIM` on the MCP server provides natural mutation
gating for UI users. If configured as `{"groups": ["deriva-mcp-mutator"]}`, only users
whose Credenza token carries that group claim can execute mutating tools -- whether they
connect via Claude Desktop, Claude Code, or this UI. No special UI-side logic required.

---

## Operating Modes

### Default-catalog mode

Activated when `DERIVA_CHATBOT_DEFAULT_HOSTNAME` and `DERIVA_CHATBOT_DEFAULT_CATALOG_ID`
are both set.

- The chat UI hides the hostname/catalog input fields
- The system prompt anchors Claude to the specific catalog:

```
You are a DERIVA data assistant for the <LABEL> catalog. You have access to tools
for querying and managing this catalog. When answering questions about data, schema,
or annotations, use the available tools rather than relying on prior knowledge.
```

- At the start of each new conversation, the UI service calls `rag_search` with a broad
  query to prime Claude's context with schema information before the user's first message
  is processed.

### General-purpose mode

Activated when the default catalog vars are unset.

- The chat UI shows hostname and catalog ID input fields (optional -- Claude can also
  ask the user for them in the conversation)
- The system prompt is broader:

```
You are a DERIVA data assistant. You have access to tools for querying and managing
DERIVA catalogs. When the user wants to work with a specific catalog, ask for the
hostname and catalog ID if they have not been provided.
```

---

## Claude Tool-Calling Loop

The chat endpoint runs the full tool-calling loop server-side and streams text chunks
to the browser via SSE. Tool invocations are not shown in the browser by default
(configurable to show "Searching..." indicators).

```python
# Pseudocode
async def chat_turn(user_message, session):
    async with mcp_client(session.bearer_token) as mcp:
        if session.tools is None:
            session.tools = to_anthropic_schema(await mcp.list_tools())

        messages = session.history + [{"role": "user", "content": user_message}]

        while True:
            async with claude.messages.stream(
                    model=settings.claude_model,
                    system=system_prompt(session),
                    tools=session.tools,
                    messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield sse_text(text)  # -> browser

                response = await stream.get_final_message()

            if response.stop_reason == "end_turn":
                break

            # execute tool_use blocks via MCP
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await mcp.call_tool(block.name, block.input)
                    tool_results.append(tool_result_block(block.id, result))

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        session.history = trim_history(messages)
```

### Tool schema conversion

MCP tool definitions include `name`, `description`, and a JSON schema for `inputSchema`.
The Anthropic API accepts `name`, `description`, and `input_schema` -- the same fields,
different key name. Conversion is a shallow rename plus passthrough of the schema object.

---

## Conversation History

History is stored server-side, keyed by the session ID (from the session cookie). In-memory
for a single-instance deployment; a Redis backend is provided for multi-instance.

History is trimmed when it approaches Claude's context limit. Two strategies (configurable):

- **Turn-count trim**: keep the last N turns (simple, predictable)
- **Token-count trim**: keep as many recent turns as fit within a token budget (more
  accurate, requires a token counter)

The system prompt is not counted as history and is always prepended fresh.

---

## Configuration

`pydantic-settings` `BaseSettings` with `DERIVA_CHATBOT_` prefix.

**Required:**

| Variable                        | Description                                                           |
|---------------------------------|-----------------------------------------------------------------------|
| `DERIVA_CHATBOT_MCP_URL`        | Base URL of the deriva-mcp-core server                                |
| `DERIVA_CHATBOT_CREDENZA_URL`   | Base URL of the Credenza instance                                     |
| `DERIVA_CHATBOT_CLIENT_ID`      | OAuth client ID registered in Credenza for this service               |
| `DERIVA_CHATBOT_CLIENT_SECRET`  | OAuth client secret                                                   |
| `DERIVA_CHATBOT_MCP_RESOURCE`   | Resource identifier for the MCP server (must match MCP server config) |
| `DERIVA_CHATBOT_PUBLIC_URL`     | Public HTTPS URL of this service (used as OAuth redirect base)        |
| `ANTHROPIC_API_KEY`             | Anthropic API key for Claude                                          |
| `DERIVA_CHATBOT_SESSION_SECRET` | Secret key for signing session cookies (random bytes, keep private)   |

**Default-catalog mode (both required to activate):**

| Variable                               | Description                                    |
|----------------------------------------|------------------------------------------------|
| `DERIVA_CHATBOT_DEFAULT_HOSTNAME`      | DERIVA server hostname for the default catalog |
| `DERIVA_CHATBOT_DEFAULT_CATALOG_ID`    | Catalog ID or alias                            |
| `DERIVA_CHATBOT_DEFAULT_CATALOG_LABEL` | Display name shown in the UI (optional)        |

**Tuning:**

| Variable                             | Default             | Description                                                                                                                                                              |
|--------------------------------------|---------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `DERIVA_CHATBOT_CLAUDE_MODEL`        | `claude-sonnet-4-6` | Claude model ID                                                                                                                                                          |
| `DERIVA_CHATBOT_MAX_HISTORY_TURNS`   | `20`                | Max conversation turns retained in server-side history                                                                                                                   |
| `DERIVA_CHATBOT_SESSION_TTL`         | `28800`             | Server-side session TTL in seconds (default 8h)                                                                                                                          |
| `DERIVA_CHATBOT_STORAGE_BACKEND`     | `memory`            | Session store backend: `memory`, `redis`, `valkey`, `postgresql`, `sqlite`                                                                                               |
| `DERIVA_CHATBOT_STORAGE_BACKEND_URL` | --                  | Connection URL for the selected backend (not used for `memory`). Examples: `redis://localhost:6379/0`, `postgresql://user:pass@host/db`, `sqlite:///path/to/sessions.db` |
| `DERIVA_CHATBOT_DEBUG`               | `false`             | Enable debug logging and show tool calls in the UI                                                                                                                       |

---

## Package Structure

```
deriva-mcp-ui/
├── pyproject.toml
├── Dockerfile
├── docs/
│   └── workplan-deriva-mcp-ui.md
└── src/
    └── deriva_mcp_ui/
        ├── __init__.py
        ├── server.py        # FastAPI app, route registration, lifespan
        ├── config.py        # Settings (DERIVA_CHATBOT_* vars)
        ├── auth.py          # Credenza OAuth client: /login, /callback, /logout routes
        ├── storage/         # Session store backends (mirrors Credenza storage pattern)
        │   ├── __init__.py  # STORAGE_BACKENDS registry + factory
        │   ├── base.py      # SessionStore protocol + Session dataclass
        │   ├── memory.py
        │   ├── redis.py
        │   ├── valkey.py
        │   ├── postgresql.py
        │   └── sqlite.py
        ├── mcp_client.py    # MCP client wrapper: connect, list_tools, call_tool
        ├── chat.py          # Claude tool-calling loop + SSE response streaming
        └── static/
            ├── index.html   # Chat UI shell
            └── chat.js      # SSE client, message rendering, login state
```

---

## Deployment

### Docker compose

Add a `deriva-mcp-ui` service to the `deriva-docker` compose stack. The service connects
to `deriva-mcp-core` over the internal Docker network (e.g., `http://deriva-mcp-core:8000`)
and to Credenza over the same network or via the public URL (depending on network topology).

Traefik routes:

- `/chatbot/` -> `deriva-mcp-ui:8001`

The MCP URL from the UI service's perspective is the internal network address. The public
URL (`DERIVA_CHATBOT_PUBLIC_URL`) is used only for the OAuth redirect URI.

### Apache (VM deployment)

```apache
ProxyPass /chatbot/ http://127.0.0.1:8001/
ProxyPassReverse /chatbot/ http://127.0.0.1:8001/
```

The UI service listens on `127.0.0.1:8001`. TLS termination is handled by Apache as with
all other services in the stack.

### Credenza client registration

A new client entry is required in `oidc_clients.json` (or equivalent Credenza config):

```json
{
  "client_id": "deriva-mcp-ui",
  "client_secret": "<secret>",
  "grant_types": [
    "authorization_code"
  ],
  "redirect_uris": [
    "https://example.org/chatbot/callback"
  ],
  "allowed_resources": [
    "<DERIVA_MCP_SERVER_RESOURCE>"
  ],
  "token_ttl": 86400
}
```

`token_ttl` controls the user bearer token lifetime. Set to a value appropriate for the
deployment (e.g., 28800 for 8h, 86400 for 24h). Users whose session expires see a
re-authentication redirect -- no data loss, the conversation history on the server side
can optionally be preserved across re-auth if keyed by a stable user identifier.

---

## Phases

---

### Phase 0 -- Scaffolding

- Create `deriva-mcp-ui` repo (or work in `deriva-mcp-core` until ready to move)
- `pyproject.toml` with `uv` / `hatchling`; dependencies: `fastapi`, `httpx`,
  `anthropic`, `mcp`, `pydantic-settings`, `itsdangerous` (session signing)
- `src/deriva_mcp_ui/` package layout
- `ruff` for lint/format, `pytest` + `pytest-asyncio` for tests
- `Dockerfile`: slim Python image, `uv sync`, `uvicorn` entrypoint
- CI scaffold

Deliverable: `uv sync` and `pytest` run cleanly (zero tests, no errors).

---

### Phase 1 -- Configuration and Auth

**1.1 Config**

`config.py`: `Settings(BaseSettings)` with all `DERIVA_CHATBOT_*` variables.
`validate()`: raise on missing required fields; also validate that if either default
catalog var is set, both are set.

**1.2 Credenza OAuth client**

`auth.py`:

- `GET /login` -- build Credenza `/authorize` URL with PKCE challenge and `state` CSRF
  token, set a short-lived cookie with the PKCE verifier and state, redirect to Credenza
- `GET /callback` -- verify state, POST to Credenza `/token` with PKCE verifier, store
  bearer token in server-side session, redirect to `/`
- `GET /logout` -- clear session, redirect to Credenza `/logout` (if supported) or to `/`
- `_require_session` dependency -- used on protected routes; redirects to `/login` if no
  valid session

**1.3 Session store**

`storage/` subpackage, mirroring the Credenza storage backend pattern:

```
storage/
  __init__.py      # STORAGE_BACKENDS registry + factory(backend, url) -> SessionStore
  base.py          # SessionStore protocol: get / set / delete / sweep
  memory.py        # In-process dict + TTL sweep (default, dev/single-instance)
  redis.py         # Redis backend (recommended for multi-instance)
  valkey.py        # Valkey backend (drop-in Redis-compatible alternative)
  postgresql.py    # PostgreSQL backend (persistent; survives restarts)
  sqlite.py        # SQLite backend (lightweight persistent; single-instance VM)
```

`Session` dataclass (defined in `base.py`): `user_id`, `bearer_token`, `history`,
`tools` (cached tool list), `created_at`, `last_active`.

The `SessionStore` protocol exposes `get(session_id)`, `set(session_id, session)`,
`delete(session_id)`, and `sweep()` (evict expired entries). All backends serialize
`Session` to JSON. The active backend is selected at startup from
`DERIVA_CHATBOT_STORAGE_BACKEND` + `DERIVA_CHATBOT_STORAGE_BACKEND_URL` via the factory.

Session ID stored in a signed `HttpOnly Secure SameSite=Lax` cookie via `itsdangerous`.

Backend guidance (same as Credenza):

- `memory` -- development and single-worker deployments only; state lost on restart
- `redis` / `valkey` -- recommended for production multi-instance deployments
- `postgresql` -- persistent; use when a PostgreSQL instance is already in the stack
- `sqlite` -- lightweight persistent option for single-instance VM deployments without Redis

Deliverable: `/login` and `/callback` complete the OAuth flow and set a session cookie.
`/logout` clears it. Protected routes redirect correctly without a session.

---

### Phase 2 -- MCP Client

`mcp_client.py`:

- `list_tools(bearer_token) -> list[AnthropicTool]` -- connect to MCP server, call
  `session.list_tools()`, convert to Anthropic tool schema format (rename `inputSchema`
  to `input_schema`, passthrough everything else), return list
- `call_tool(bearer_token, name, arguments) -> str` -- connect, call
  `session.call_tool(name, arguments)`, extract text content from result, return as string
- Both functions open a fresh `streamablehttp_client` connection per call (stateless HTTP
  model; no persistent connection to maintain)
- Connection errors (MCP server unreachable, 401 from MCP server) raise typed exceptions
  that the chat layer converts to user-visible error messages

Deliverable: unit tests with a mocked MCP server confirming tool listing and tool call
round-trips.

---

### Phase 3 -- Claude Integration and Chat Endpoint

`chat.py`:

- `system_prompt(session) -> str` -- returns catalog-anchored prompt or general prompt
  depending on config; if default-catalog mode, primes schema context via `rag_search`
  at the start of the first turn (result appended to system prompt, not shown to user)
- `run_chat_turn(user_message, session, bearer_token) -> AsyncIterator[str]` -- the
  full tool-calling loop; yields SSE-formatted text chunks as Claude generates them;
  calls `mcp_client.call_tool()` for each `tool_use` block; appends completed turn to
  `session.history`; trims history at `MAX_HISTORY_TURNS`
- `trim_history(messages, max_turns) -> list` -- keep system messages untouched, trim
  oldest user/assistant pairs

`server.py`:

- `GET /` -- serve `static/index.html` (redirects to `/login` if no session)
- `POST /chat` -- accepts `{"message": "..."}` JSON body; calls `run_chat_turn`;
  returns `text/event-stream` SSE response streaming text chunks; final event is
  `event: done`
- `GET /session-info` -- returns `{"user_id": ..., "catalog_mode": ..., "label": ...}` for
  the JS to render in the UI header

Deliverable: end-to-end chat against a live (or mocked) MCP server, streamed to a
`curl` or `httpie` client.

---

### Phase 4 -- Browser UI

`static/index.html` and `static/chat.js`:

- Clean, minimal chat layout: message thread, input box, send button
- SSE client using `EventSource` or `fetch` with streaming body (prefer `fetch` for
  POST support; `EventSource` only supports GET)
- Message rendering: user messages right-aligned, assistant messages left-aligned with
  Markdown rendering (use a lightweight library like `marked.js` -- no framework needed)
- Login state: if the server returns 401 on `/chat`, redirect to `/login`
- Loading indicator while Claude is thinking (before first SSE chunk arrives)
- Catalog label in the header (populated from `/session-info`)
- In general-purpose mode: hostname and catalog ID fields shown above the input box;
  values passed in the `POST /chat` body and stored in the session

No build step. Plain HTML + vanilla JS + one CDN script for Markdown rendering.

Deliverable: functional browser UI against a running stack.

---

### Phase 5 -- Default Catalog Schema Priming

When default-catalog mode is active, the first turn of a new conversation triggers a
`rag_search` call against the catalog's schema before passing the user's message to
Claude. The result is appended to the system prompt (not shown in the chat thread) so
Claude has immediate awareness of available tables and columns.

If RAG is not enabled on the MCP server (`rag_status` returns no schema entries for the
catalog), fall back to `get_schema` on the default catalog's first schema and include
the result in the system prompt directly. Truncate to a reasonable token budget if the
schema is very large.

This priming happens once per conversation, not on every turn.

Deliverable: in default-catalog mode, Claude answers schema questions correctly on the
first turn without the user needing to say "look at the schema first."

---

### Phase 6 -- Deployment Wiring

- `Dockerfile`: multi-stage build; final image runs `uvicorn deriva_mcp_ui.server:app`
- `deriva-docker` compose service entry with env var passthrough
- Traefik label config for `/chatbot/` path routing
- Apache `ProxyPass` snippet for VM deployments (documented, not automated)
- Credenza client registration documented with all required fields and recommended TTL values
- `README.md`: quickstart, configuration reference, deployment options

Deliverable: running in the `deriva-docker` compose stack with a real Credenza login.

---

### Phase 7 -- Hardening

- All non-memory storage backends tested and documented; backend selection via
  `DERIVA_CHATBOT_STORAGE_BACKEND` + `DERIVA_CHATBOT_STORAGE_BACKEND_URL`
- Re-authentication flow: detect 401 from MCP server mid-conversation, preserve
  conversation history in the session, redirect to `/login`, resume after re-auth
- Input length limit on user messages (prevent runaway context / cost)
- Rate limiting per session (e.g., max N requests per minute) using a simple token bucket
  in the session store
- `DERIVA_CHATBOT_DEBUG=true`: show tool calls in the chat thread as collapsible blocks
  (useful for debugging, off by default in production)
- Structured logging (JSON, same format as `deriva-mcp-core`)
- `/health` endpoint (no auth, for Docker health probes)

---

## Out of Scope

- Multi-user conversation sharing or persistence across browser sessions (each session
  is private to the authenticated user)
- Direct Hatrac file upload/download via the UI (tool calls can reference Hatrac paths
  but the UI does not handle binary transfers)
- Customizable system prompts per-user (operator-set only via config)
- WebSocket transport (SSE over HTTP is sufficient and simpler to proxy)
- Any modification to `deriva-mcp-core` -- the MCP server is consumed as-is