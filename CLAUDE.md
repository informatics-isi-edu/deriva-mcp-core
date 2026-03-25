# deriva-mcp-core -- Claude Code Instructions

## Commands

Always run tools through `uv run` -- never invoke `pytest` or `ruff` directly.

```bash
uv run pytest                        # run all tests
uv run pytest tests/test_foo.py -v   # run one file
uv run pytest -m "not integration"   # skip integration tests (default)
uv run ruff check src tests          # lint
uv run ruff format src tests         # format
uv run ruff check --fix src tests    # fix auto-fixable lint errors
```

Coverage report:

```bash
uv run pytest --cov --cov-report=term-missing
```

## Package Layout

Source lives under `src/deriva_mcp_core/`. The test suite is in `tests/`.
Import paths always start from `deriva_mcp_core`, never relative.

## Coding Conventions

- **ASCII only in code** -- no Unicode characters in docstrings or comments.
  En-dashes, curly quotes, non-breaking hyphens, bullet characters, and
  box-drawing characters are all prohibited. Plain ASCII hyphens and
  asterisks only. Unicode is fine in `.md` files and in code/test data strings.
- **No "raw" in names** -- do not use "raw" in variable names, parameter names,
  or docstrings.
- **No backwards-compat shims** -- if something is unused, delete it.
- **No over-engineering** -- only add what the current phase requires.

## Architecture Landmarks

- `context.py` -- per-request `contextvars.ContextVar`; `set_current_credential()`
  (internal), `get_request_credential()` / `get_catalog()` / `get_hatrac_store()`
  (public API, exported from `__init__.py`)
- `config.py` -- `Settings(BaseSettings)` with `DERIVA_MCP_` prefix;
  `settings.validate_for_http()` raises on missing required fields (call at
  HTTP transport startup only)
- `auth/` -- introspect, exchange, token_cache, verifier (Phases 2-3)
- `plugin/` -- `PluginContext` wrapping FastMCP; entry-point loader
- `tools/` -- built-in DERIVA tool modules, each with `register(ctx: PluginContext)`
- `server.py` -- `create_server()` factory + CLI entrypoint (Phase 3)

## Tool Implementation Rules

### deriva_call() wrapper

Every tool function that calls into ERMrest or Hatrac must wrap the DERIVA call
in `with deriva_call():` from `..context`. This context manager catches 401
responses, evicts the stale derived token for the current principal, and
re-raises so the MCP framework surfaces the error to the caller.

```python
from ..context import deriva_call, get_catalog

with deriva_call():
    catalog = get_catalog(hostname, catalog_id)
    # ... all DERIVA calls inside the block
```

The block should contain only the DERIVA I/O. Post-processing (building the
return dict, emitting audit events) goes outside it.

### Audit logging for mutating tools

Every tool registered with `mutates=True` must emit an `audit_event` on both
success and failure. Import from `..telemetry`:

```python
from ..telemetry import audit_event
```

Rules:
- **Success event:** named `<module>_<operation>` (e.g. `schema_create_table`,
  `annotation_set_visible_columns`). Include `hostname`, `catalog_id`, and
  any targeting fields that identify the object mutated (`schema`, `table`,
  `column`, `annotation_tag`). Do NOT include the payload value being written.
- **Failure event:** same name with `_failed` suffix. Include the same
  targeting fields plus `error_type=type(exc).__name__`. Emit inside the
  `except` block, after `logger.error(...)`.
- `principal` and `timestamp` are auto-injected by `audit_event()`; do not
  pass them manually unless overriding (e.g. pre-auth events in verifier.py).
- Read-only tools (`mutates=False`) do not need audit events.

Example (from entity.py):

```python
audit_event(
    "entity_insert",
    hostname=hostname,
    catalog_id=catalog_id,
    schema=schema,
    table=table,
    input_row_count=len(entities),
    inserted_count=len(inserted),
)
```

### Mutation kill switch

Register every tool with an explicit `mutates=` kwarg -- `ctx.tool(mutates=True)`
or `ctx.tool(mutates=False)`. Omitting it raises `TypeError` at server startup.
The kill switch (`DERIVA_MCP_DISABLE_MUTATING_TOOLS=true`) blocks all
`mutates=True` tools before they execute; they return `{"error": "... disabled ..."}`.

## Testing Tool Modules

Tool functions are local to their `register(ctx)` call (closures). Tests use a
`_CapturingMCP` fixture (in `test_tools.py`) that stores registered functions by
name: `ctx._mcp.tools["tool_name"]`. Call them directly in tests.

`mock_catalog` fixture sets `catalog.get.side_effect` to return different responses
for `/schema` vs other paths. Entity/query tests that override `get.return_value`
must first set `mock_catalog.get.side_effect = None` to clear the side_effect.

## Workplan

`docs/workplan-deriva-mcp-core.md` is the authoritative phase-by-phase plan.
Check it first when starting a new phase.