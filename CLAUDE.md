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
  (internal), `get_request_credential()` / `get_deriva_server()` / `get_hatrac_store()`
  (public API, exported from `__init__.py`)
- `config.py` -- `Settings(BaseSettings)` with `DERIVA_MCP_` prefix;
  `settings.validate_for_http()` raises on missing required fields (call at
  HTTP transport startup only)
- `auth/` -- introspect, exchange, token_cache, verifier (Phases 2-3)
- `plugin/` -- `PluginContext` wrapping FastMCP; entry-point loader
- `tools/` -- built-in DERIVA tool modules, each with `register(ctx: PluginContext)`
- `server.py` -- `create_server()` factory + CLI entrypoint (Phase 3)

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