# ADR-0001: Async-First Architecture (ASGI/uvicorn)

**Status:** Accepted
**Date:** 2026-05-04
**Applies to:** deriva-mcp-core, deriva-mcp-ui

---

## Context

The MCP protocol (Model Context Protocol) is built on JSON-RPC 2.0 over streaming
transports -- Server-Sent Events for HTTP and stdio for local process communication.
Both transports are inherently streaming and connection-oriented: the server must be
able to push notifications and intermediate results to the client without closing the
connection, and the client must be able to receive them concurrently with sending
further requests.

The reference Python implementation of MCP (`mcp` SDK, used by FastMCP) is
**async-only**. There is no sync client or server API. Every tool invocation, resource
read, and prompt call is an awaitable operation. The streaming transport layer
requires a running event loop for the lifetime of the connection.

The DERIVA MCP stack consists of two services:

- **deriva-mcp-core** -- an MCP *server* built on FastMCP. It IS the async server
  framework; the async runtime is not an optional wrapper but the execution model
  the service is built on.
- **deriva-mcp-ui** -- a browser chatbot that uses the `mcp` SDK as an MCP *client*
  to drive tool calls against deriva-mcp-core. Every tool call dispatches through the
  async MCP client transport.

Both services are therefore async at their core, independent of any web framework
preference.

## Decision

deriva-mcp-core and deriva-mcp-ui use **ASGI** (Async Server Gateway Interface) as
their Python web layer, served by **uvicorn**. This is a direct consequence of the
MCP SDK dependency and is not a stylistic or fashionable choice.

- deriva-mcp-core: FastMCP exposes an ASGI app directly. No alternative exists.
- deriva-mcp-ui: FastAPI is used as the web framework. It composes naturally with
  async MCP client calls inside route handlers.

**Credenza** (the OIDC session broker) has no MCP dependency. It remains Flask (WSGI)
and is deployed via mod_wsgi, consistent with the broader DERIVA ecosystem of
Apache-hosted services. This divergence is intentional and correct.

## Consequences

### Deployment

These two services cannot run under **mod_wsgi**, which is a WSGI-only in-process
Apache module. The supported deployment models are:

- **Docker (primary):** uvicorn runs inside a container on an internal Docker network.
  Traefik handles TLS termination and path-based routing via container labels. No
  host port is exposed; network isolation prevents clients from bypassing the proxy.
- **Bare-metal / VM (theoretical):** uvicorn runs as a systemd service bound to a
  non-public interface. Apache proxies to it via `mod_proxy_http`. This is
  architecturally equivalent to the Docker case -- Apache is the sole ingress.
  A further option is to run Docker on the same host and use the compose stack,
  which avoids the need to manage uvicorn as a separate service.

An ASGI-to-WSGI bridge (e.g. `a2wsgi`) is not viable because both services use
streaming responses (SSE for the chat endpoint, streaming MCP transport). Bridges
buffer the response body, which breaks streaming.

### Observability

uvicorn does not integrate with Apache's built-in logging pipeline. Both services
emit structured JSON audit logs to syslog (LOCAL1) and route application logs to
stderr. In the Docker stack, the compose `syslog` log driver forwards container
output to a dedicated rsyslog container; other observability services in the stack
are configured to read from that rsyslog instance. On AWS deployments the same driver
routes to CloudWatch. On bare metal with systemd (theoretical), `journald` captures
stdout/stderr and the structured audit stream goes to the host rsyslog directly.

### Async discipline in deriva-mcp-core

Because tool handlers run in an async context, all I/O inside them must be
non-blocking. Blocking calls (requests, synchronous database drivers, time.sleep)
must not be made directly from async handlers -- they would stall the event loop
and starve concurrent requests. See `docs/plugin-authoring-guide.md` for the
required patterns (httpx, asyncio.sleep, run_in_executor for unavoidable blocking
calls).

The `stateless_http=True` FastMCP constructor flag is required to work around a
contextvar propagation bug in stateful mode where tool handlers inherit stale derived
tokens after a token refresh. See workplan for details.

## Alternatives Considered

### Flask + sync MCP wrapper

Rewriting deriva-mcp-ui as Flask (WSGI) does not eliminate the async boundary. The
`mcp` SDK client is async-only. Calling it from sync Flask handlers would require
either `asyncio.run()` per call (creates/destroys an event loop on every tool
invocation, loses connection pooling) or a persistent background event loop thread
that handlers dispatch into. Both approaches are more complex and less correct than
the current architecture where async composes end-to-end.

For deriva-mcp-core the option does not exist at all: the service IS a FastMCP server.
Replacing FastMCP with a sync server would mean reimplementing the MCP protocol.

### gunicorn + UvicornWorker

Gunicorn can manage uvicorn workers via `UvicornWorker`, providing a familiar process
manager for teams accustomed to the gunicorn operational model. This is viable for
bare-metal deployment but still requires `mod_proxy` (not `mod_wsgi`) and adds a
process management layer with no functional benefit over uvicorn + systemd for this
workload scale.

### Custom XFF extraction

deriva-mcp-ui uses `uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware`,
enabled via `DERIVA_CHATBOT_BEHIND_PROXY=true`, to rewrite `request.client` with
the real client IP before audit events are emitted.  Since uvicorn is non-negotiable
(see above), this is the correct mechanism -- no custom `X-Forwarded-For` parsing
is needed.