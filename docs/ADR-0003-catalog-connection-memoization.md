# ADR-0003: Catalog Connection Memoization (Deferred)

**Status:** Deferred

**Date:** 2026-09-01

**Applies to:** deriva-mcp-core

---

## Context

A bug report ("deriva-mcp-core reconnects and re-introspects the catalog on
every tool call") observed multi-minute `list_schemas`/`get_catalog_info`
calls against a live catalog and proposed memoizing the `ErmrestCatalog`
connection object as the fix. This ADR records why that wider proposal was
evaluated and not built, so it does not get silently re-proposed without the
analysis below.

`context.py:get_catalog()` builds a brand-new `DerivaServer(...).connect_ermrest()`
on every call -- confirmed by inspection, no memoization exists. Constructing
that object does no network I/O (`ErmrestCatalog.connect()` in deriva-py is
pure string parsing and attribute assignment), so the cost is entirely in the
first `GET /schema` made through it: ERMrest regenerates the schema document
from scratch server-side on every request rather than caching it.

deriva-py's own `DerivaBinding` (the base class of `ErmrestCatalog`) already
has a per-object HTTP cache (`self._cache`, keyed by URL) that sends
`If-None-Match` on repeat GETs and can turn a full regeneration into a cheap
`304`. Because `get_catalog()` constructs a fresh object with an empty cache
on every call, that mechanism never gets a chance to help. The reported
~100x speedup on a repeat fetch (232s -> 2.3s) was exactly this mechanism
working on a connection that happened to survive between two calls in
isolation testing.

The bug actually reported (multi-minute schema-introspection calls) was
resolved separately, without touching connection objects: `tools/catalog.py`
now caches the fetched *schema JSON* per `(hostname, catalog_id, user_id)`
with a TTL (`DERIVA_MCP_SCHEMA_CACHE_TTL_SECONDS`, default 900s), invalidated
immediately on schema-mutating tool calls via the existing `on_schema_change`
plugin hook. That fix is scoped to the four schema-introspection tools
(`list_schemas`, `get_catalog_info`, `get_schema`, `get_table`) and the
`deriva://catalog/.../schema` resource.

This ADR is about the broader idea the bug report proposed: memoizing the
`ErmrestCatalog` connection object itself, which would also benefit entity,
query, and Hatrac tools that go through `get_catalog()` but don't hit the
schema cache.

## Decision

We are not implementing catalog connection memoization at this time. There is
no reported symptom on entity, query, or Hatrac tools -- only on schema
introspection, which is already fixed. Building this now would be solving a
problem nobody has hit yet.

If it is revisited, the design should be:

- Keyed by `(hostname, catalog_id, principal)`, not just `(hostname,
  catalog_id)` -- the cached object holds an authenticated session, so an
  unscoped cache would let one principal's request use another's connection.
- Eviction wired into `context.py:deriva_call()`'s existing 401 handler
  (`invalidate_current_derived_token()`), alongside the `DerivedTokenCache`
  eviction that already happens there -- see Consequences below for why this
  is required, not optional.
- A TTL capped at or below the derived-token expiry, as defense in depth if
  the 401-eviction path has a gap.
- Bounded size (LRU or similar) on both the new per-process cache and the
  connection object's own unbounded internal `_cache` dict (deriva-py has no
  eviction logic there at all -- confirmed by inspection, it grows for the
  life of the object).

## Consequences

### Upside, if built

Deriva-py's conditional-GET cache would survive across calls for any
endpoint hit with an identical URL through the same cached object, not just
`/schema`. This is a real win, but concentrated on repeat-identical-URL
calls (schema/table metadata lookups) -- most entity and query tool calls
vary by filter, RID, or pagination each time, so they would see cache misses
as often as they do today. There is also a smaller, unconditional win from
reusing the underlying `requests.Session` (TCP/TLS keep-alive) instead of a
fresh handshake per call.

### Downside

- **Credential staleness.** `ErmrestCatalog`/`DerivaBinding.set_credentials()`
  bakes the `Authorization` header into the session once, at construction.
  `DerivedTokenCache` already rotates derived tokens per principal on
  expiry/401 (`token_cache.py`), but a cached connection object would keep
  sending its original header regardless -- nothing re-runs
  `set_credentials()`. Without the eviction wiring described in the
  Decision, this reintroduces exactly the class of bug
  `deriva_call()`/`DerivedTokenCache` was built to prevent: silent 401s that
  never self-heal, because the token cache evicts and refreshes but the
  connection cache keeps handing back a connection carrying the old header.
- **Unbounded memory.** deriva-py's per-object `_cache` dict has no eviction
  of any kind. Stacked with a new per-process connection cache, that's two
  unbounded structures growing for the life of the server across every
  catalog and principal it has ever served.
- **Blast radius.** Unlike the schema-JSON cache, this changes caching
  behavior for every DERIVA-backed tool -- entity CRUD, query, Hatrac -- not
  just four read-only introspection tools. More surface area to reason about
  and to break in ways that are hard to notice (a stale connection doesn't
  crash, it just serves subtly wrong data or 401s intermittently).
  Concurrent use of one cached object's session/cache dict by two overlapping
  requests for the same principal is also untested territory this codebase
  has never had to think about, since every call currently gets its own
  object.

### Trigger for reconsidering

Revisit this if entity, query, or Hatrac tools show the same
multi-call-latency symptom against a real workload that the schema cache
does not cover. Until then, the schema-JSON cache in `tools/catalog.py`
is the complete fix for the reported problem.