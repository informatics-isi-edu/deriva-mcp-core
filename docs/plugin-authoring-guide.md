# Plugin Authoring Guide

This guide covers writing an external plugin for `deriva-mcp-core`: registering
tools, using lifecycle hooks, extending the RAG subsystem, and testing.

## Contents

- [Overview](#overview)
- [Package Setup](#package-setup)
- [The register Function](#the-register-function)
- [Registering Tools](#registering-tools)
    - [Credential Access](#credential-access)
    - [Handling Downstream 401s](#handling-downstream-401s)
    - [Audit Logging for Mutating Tools](#audit-logging-for-mutating-tools)
- [Lifecycle Hooks](#lifecycle-hooks)
- [Background Tasks](#background-tasks)
- [RAG Extension](#rag-extension)
    - [Declaring a Documentation Source](#declaring-a-documentation-source)
    - [Indexing Catalog Data](#indexing-catalog-data)
    - [Custom Row Serialization](#custom-row-serialization)
- [Testing](#testing)
- [Import Patterns](#import-patterns)

---

## Overview

Plugins extend `deriva-mcp-core` without modifying it. A plugin package declares a
`register(ctx)` function as a Python entry point in the `deriva_mcp.plugins` group.
At server startup, `create_server()` discovers all installed plugins (subject to
`DERIVA_MCP_PLUGIN_ALLOWLIST`) and calls each `register` function with a
`PluginContext` instance.

The `PluginContext` exposes the same API used by the built-in tool modules -- no
special access is needed beyond what any built-in tool uses.

**Plugins are for domain logic, not DERIVA primitives.** The built-in tools already
cover generic catalog operations: schema introspection, entity CRUD, attribute
queries, annotations, vocabulary, and Hatrac. A plugin should add tools that express
domain-specific concepts on top of those primitives -- for example, a DerivaML plugin
would expose Execution and Dataset workflows rather than re-implementing `get_entities`
or `query_attribute`. Before writing a new tool, check whether a built-in tool already
covers the operation; the LLM can compose built-in tools itself if given the right
context via RAG or prompts.

---

## Package Setup

```toml
# pyproject.toml
[project]
name = "my-deriva-plugin"
dependencies = [
    "deriva-mcp-core>=0.1",
]

[project.entry-points."deriva_mcp.plugins"]
my-plugin = "my_package.mcp_plugin:register"
```

The entry point name (`my-plugin`) is what operators use with
`DERIVA_MCP_PLUGIN_ALLOWLIST`. The value is `module:callable`.

Install alongside `deriva-mcp-core`:

```bash
uv pip install my-deriva-plugin
```

The plugin is loaded automatically on next server start. No server configuration
changes are required unless the operator has set an explicit `PLUGIN_ALLOWLIST`.

---

## The register Function

`register(ctx)` is called once at server startup. It declares tools, hooks, and RAG
sources by calling methods on the `PluginContext`. The function must be synchronous.

```python
# my_package/mcp_plugin.py
from deriva_mcp_core.plugin.api import PluginContext


def register(ctx: PluginContext) -> None:
    # Register tools, hooks, RAG sources here.
    # All registrations are additive -- safe to call multiple times on
    # the same ctx (e.g., from separate entry points in a split package).
    ...
```

### Split-package pattern

If your plugin ships tools and RAG components from separate repositories or packages,
each declares its own entry point and `register` function. Both functions receive the
same `PluginContext` at startup and their registrations are additive:

```toml
# tools package
[project.entry-points."deriva_mcp.plugins"]
my-plugin = "my_package.tools:register"

# rag package (separate repo, separate install)
[project.entry-points."deriva_mcp.plugins"]
my-plugin-rag = "my_package_rag.rag:register"
```

---

## Registering Tools

Use `@ctx.tool(mutates=True|False)` to register MCP tools. The `mutates=` keyword
is required -- omitting it raises `TypeError` at server startup.

```python
def register(ctx: PluginContext) -> None:
    @ctx.tool(mutates=False)
    async def my_read_tool(hostname: str, catalog_id: str, schema: str) -> str:
        """Fetch all rows from MyTable in the given schema."""
        import json
        from deriva_mcp_core import deriva_call, get_catalog
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                rows = catalog.get(f"/entity/{schema}:MyTable").json()
            return json.dumps({"rows": rows})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=True)
    async def my_write_tool(
            hostname: str,
            catalog_id: str,
            schema: str,
            rid: str,
            description: str,
    ) -> str:
        """Update the Description field on a MyTable row."""
        import json
        from deriva_mcp_core import deriva_call, get_catalog
        from deriva_mcp_core.telemetry import audit_event
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                pb = catalog.getPathBuilder()
                path = pb.schemas[schema].tables["MyTable"]
                path.filter(path.RID == rid).update(
                    [{"RID": rid, "Description": description}]
                )
            audit_event(
                "my_plugin_update_description",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                rid=rid,
            )
            return json.dumps({"updated": True})
        except Exception as exc:
            audit_event(
                "my_plugin_update_description_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                schema=schema,
                rid=rid,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": str(exc)})
```

**`mutates=True`** -- tool writes to the DERIVA catalog or Hatrac. Blocked
automatically when `DERIVA_MCP_DISABLE_MUTATING_TOOLS=true` or when the caller
does not satisfy `DERIVA_MCP_MUTATION_REQUIRED_CLAIM`. Your tool body does not
need to check either condition -- the guard runs before your function is called.

**`mutates=False`** -- tool is read-only (including writes to the local vector
store, which is not the DERIVA catalog). Not affected by the mutation kill switch.

### Credential Access

Three functions provide access to the current request's credential. Import from
`deriva_mcp_core`:

| Function                            | Returns          | Use when                                                                                       |
|-------------------------------------|------------------|------------------------------------------------------------------------------------------------|
| `get_catalog(hostname, catalog_id)` | `ErmrestCatalog` | Making ERMrest calls (schema introspection, entity CRUD, attribute queries)                    |
| `get_hatrac_store(hostname)`        | `HatracStore`    | Making Hatrac object store calls                                                               |
| `get_request_credential()`          | `dict`           | Passing credentials to a higher-level API that constructs its own HTTP client (e.g., DerivaML) |

```python
# ERMrest
from deriva_mcp_core import get_catalog

catalog = get_catalog(hostname, catalog_id)

# Hatrac
from deriva_mcp_core import get_hatrac_store

store = get_hatrac_store(hostname)

# Higher-level API (e.g., DerivaML)
from deriva_mcp_core import get_request_credential

cred = get_request_credential()
ml = DerivaML(hostname, catalog_id, credential=cred)
```

All three read from the per-request contextvar set by the auth middleware. They
must be called from within a tool or resource handler (not at module or
`register()` level).

In stdio mode, `get_catalog` and `get_hatrac_store` read from
`~/.deriva/credential.json` automatically. `get_request_credential()` returns the
same credential dict. No code changes are needed to support both modes.

### Handling Downstream 401s

Wrap all ERMrest and Hatrac calls in `with deriva_call():`. This context manager
intercepts HTTP 401 responses, evicts the stale derived token from the cache, and
re-raises so the next MCP request gets a fresh token exchange.

```python
from deriva_mcp_core import deriva_call, get_catalog

try:
    with deriva_call():
        catalog = get_catalog(hostname, catalog_id)
        result = catalog.get("/entity/public:MyTable").json()
    return json.dumps({"rows": result})
except Exception as exc:
    return json.dumps({"error": str(exc)})
```

The block should contain only DERIVA I/O. Post-processing (building the return
dict, emitting audit events) goes outside it.

### Audit Logging for Mutating Tools

Every `mutates=True` tool must emit audit events on both success and failure.
Import `audit_event` from `deriva_mcp_core.telemetry`:

```python
from deriva_mcp_core.telemetry import audit_event
```

Naming conventions:

- **Success:** `<plugin_name>_<operation>` (e.g., `myml_create_execution`)
- **Failure:** same name with `_failed` suffix

Include `hostname`, `catalog_id`, and any fields that identify the object being
mutated. Do not include payload values (content of updated fields). On failure,
include `error_type=type(exc).__name__`.

`principal` and `timestamp` are injected automatically -- do not pass them.

Read-only tools (`mutates=False`) do not need audit events.

---

## Lifecycle Hooks

Hooks let your plugin react to catalog and schema events without polling.
Register them from `register(ctx)`:

```python
def register(ctx: PluginContext) -> None:
    async def _on_catalog_connect(
            hostname: str,
            catalog_id: str,
            schema_hash: str,
            schema_json: dict,
    ) -> None:
        # Called after any tool accesses a catalog.
        # schema_json is the full /schema response already in memory.
        # schema_hash is a SHA-256 fingerprint (16-char hex) of schema_json.
        ...

    async def _on_schema_change(hostname: str, catalog_id: str) -> None:
        # Called after any tool mutates the catalog schema
        # (create_table, add_column, set_table_description, etc.).
        ...

    ctx.on_catalog_connect(_on_catalog_connect)
    ctx.on_schema_change(_on_schema_change)
```

Hooks are dispatched fire-and-forget (`asyncio.create_task`). Exceptions in hooks
are logged and suppressed -- a failing hook never causes a tool call to fail.

**`on_catalog_connect(hostname, catalog_id, schema_hash, schema_json)`**

Fires whenever any tool connects to a catalog (including the first access from
`get_catalog()`). The `schema_json` dict is already in memory; the hook receives
it directly at no extra cost. Use this to trigger data indexing when a user first
accesses a catalog.

**`on_schema_change(hostname, catalog_id)`**

Fires after any built-in DDL tool succeeds (`create_table`, `add_column`,
`set_table_description`, `set_column_description`, `set_column_nullok`). The hook
does not receive the new schema; fetch it if needed via `get_catalog()`.

---

## Background Tasks

Use `ctx.submit_task(coroutine, name)` for operations that may take longer than a
few seconds. It captures the current principal and bearer token, registers the task
with the `TaskManager`, and returns a `task_id` string immediately. The LLM can
poll with `get_task_status(task_id)`.

```python
@ctx.tool(mutates=True)
async def my_long_operation(hostname: str, catalog_id: str) -> str:
    import json
    from deriva_mcp_core.telemetry import audit_event

    task_id = ctx.submit_task(
        _do_long_operation(hostname, catalog_id),
        name=f"my_long_operation {catalog_id}",
        description="Detailed description for the LLM",
    )
    audit_event("my_plugin_long_op_submitted", hostname=hostname,
                catalog_id=catalog_id, task_id=task_id)
    return json.dumps({"task_id": task_id, "status": "submitted"})


async def _do_long_operation(hostname: str, catalog_id: str) -> dict:
    # This coroutine runs as a background task.
    # It does NOT have access to the per-request contextvar credential --
    # call get_credential(task_id) explicitly before each DERIVA operation.
    from deriva_mcp_core import get_catalog
    from deriva_mcp_core.tasks import get_task_manager

    manager = get_task_manager()
    # task_id is not directly available here; capture it at submit time if needed.
    # For simple cases, re-use get_catalog with the credential from the manager.
    # See note below.
    catalog = get_catalog(hostname, catalog_id)
    # ... long-running work ...
    return {"result": "done"}
```

**Credential re-exchange for long tasks**

Derived tokens expire after 30 minutes. For tasks that may run longer, fetch a
fresh credential from the `TaskManager` before each batch of DERIVA operations:

```python
async def _do_long_operation(task_id: str, hostname: str, catalog_id: str) -> dict:
    from deriva.core import DerivaServer
    from deriva_mcp_core.tasks import get_task_manager

    manager = get_task_manager()
    # Fetch credential (re-exchanges automatically if near expiry)
    cred = await manager.get_credential(task_id)
    catalog = DerivaServer("https", hostname, credentials=cred).connect_ermrest(catalog_id)
    # ... batch 1 ...

    cred = await manager.get_credential(task_id)  # refresh before next batch
    # ... batch 2 ...
    return {"result": "done"}
```

To pass `task_id` into the coroutine, use a closure or pass it as a parameter:

```python
async def _build_task_coro(hostname, catalog_id):
    task_id_ref = [None]

    async def _run():
        task_id = task_id_ref[0]
        ...

    return _run, task_id_ref


coro, ref = await _build_task_coro(hostname, catalog_id)
task_id = ctx.submit_task(coro(), name="my task")
ref[0] = task_id  # safe under asyncio cooperative scheduling
```

**Synchronous work in threads**

Wrap blocking operations with `asyncio.to_thread()` so they do not block the
event loop. Fetch credentials in the async wrapper before entering the thread --
`get_credential` is async and cannot be called from inside the thread:

```python
async def _do_sync_work(task_id: str, dataset_path: str, params: dict) -> dict:
    from deriva_mcp_core.tasks import get_task_manager
    manager = get_task_manager()
    cred = await manager.get_credential(task_id)
    result = await asyncio.to_thread(_run_sync, dataset_path, params, cred)
    return result


def _run_sync(dataset_path: str, params: dict, cred: dict) -> dict:
    # Runs in a thread pool. No async allowed.
    ...
    return {"result": "done"}
```

---

## RAG Extension

The RAG subsystem is optional (`DERIVA_MCP_RAG_ENABLED=false` by default).
Registration calls in `register(ctx)` are always safe -- they are no-ops when RAG
is disabled, so plugins need no RAG guard logic.

### Declaring a Documentation Source

Register a GitHub documentation source with `ctx.rag_source()`. The server crawls
it incrementally on startup (`DERIVA_MCP_RAG_AUTO_UPDATE=true`) and indexes any
changed Markdown files:

```python
def register(ctx: PluginContext) -> None:
    ctx.rag_source(
        name="my-plugin-docs",  # unique name; must not conflict with built-ins
        repo_owner="my-org",
        repo_name="my-plugin",
        branch="main",
        path_prefix="docs/",  # only .md files under this path are indexed
        doc_type="user-guide",  # tag stored in vector metadata
    )
```

Plugin-declared sources take precedence over runtime-added sources
(`rag_add_source` tool) on name conflict. Built-in source names (`deriva-py-docs`,
`ermrest-docs`, `chaise-docs`) are reserved.

Sources declared via `ctx.rag_source()` cannot be removed with `rag_remove_source`
-- that tool only manages runtime-added sources.

### Indexing Catalog Data

Use `index_table_data()` from a `on_catalog_connect` hook to index domain-specific
table rows for semantic search. This is the primary integration point for plugins
that want users to search their data:

```python
from deriva_mcp_core.rag.data import RowSerializer, data_source_name, index_table_data


def register(ctx: PluginContext) -> None:
    async def _on_catalog_connect(
            hostname: str,
            catalog_id: str,
            schema_hash: str,
            schema_json: dict,
    ) -> None:
        from deriva_mcp_core import get_catalog
        from deriva_mcp_core.rag import get_rag_store

        store = get_rag_store()
        if store is None:
            return  # RAG disabled

        # Avoid reindexing on every connect (staleness TTL defaults to 3600s)
        user_id = _get_user_id()  # see note below
        source = data_source_name(hostname, catalog_id, user_id)
        if await store.has_source(source):
            return  # already indexed and fresh

        catalog = get_catalog(hostname, catalog_id)
        rows = catalog.get("/entity/public:Dataset").json()
        await index_table_data(
            store=store,
            hostname=hostname,
            catalog_id=catalog_id,
            table_name="Dataset",
            rows=rows,
            user_id=user_id,
            serializer=MyDatasetSerializer(),
        )

    ctx.on_catalog_connect(_on_catalog_connect)
```

Hooks run as fire-and-forget tasks. Import `get_request_user_id` inside the hook
(not at `register()` level) so it resolves at call time from the active request
context:

```python
async def _on_catalog_connect(hostname, catalog_id, schema_hash, schema_json):
    from deriva_mcp_core.context import get_request_user_id
    user_id = get_request_user_id()
    ...
```

### Custom Row Serialization

Subclass `RowSerializer` to produce richer Markdown for domain-specific tables.
Return `None` for rows the plugin does not handle; the generic serializer
(`## TableName: RID` + `**Column:** value` lines) is used as a fallback:

```python
from deriva_mcp_core.rag.data import RowSerializer


class MyDatasetSerializer(RowSerializer):
    def serialize(self, table_name: str, row: dict) -> str | None:
        if table_name == "Dataset":
            title = row.get("Title", "")
            rid = row.get("RID", "")
            status = row.get("Status", "")
            desc = row.get("Description", "")
            return (
                f"## Dataset: {title} (RID: {rid})\n\n"
                f"**Status:** {status}\n\n"
                f"{desc}"
            )
        return None  # generic rendering for all other tables
```

---

## Testing

Plugin tool functions are closures defined inside `register(ctx)`. Tests need
a `PluginContext` backed by a minimal MCP stand-in that stores registered
functions by name for direct invocation.

### Fixtures

Copy this infrastructure into your plugin's `conftest.py`:

```python
# tests/conftest.py
from typing import Any
import pytest
from unittest.mock import MagicMock
from deriva_mcp_core.plugin.api import PluginContext, _set_plugin_context


class _CapturingMCP:
    """Minimal FastMCP stand-in that stores registered tools by name."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **kwargs: Any):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def resource(self, *a: Any, **kw: Any):
        return lambda fn: fn

    def prompt(self, *a: Any, **kw: Any):
        return lambda fn: fn


@pytest.fixture()
def mcp():
    return _CapturingMCP()


@pytest.fixture()
def ctx(mcp):
    _ctx = PluginContext(mcp)
    _set_plugin_context(_ctx)
    yield _ctx
    _set_plugin_context(None)


@pytest.fixture()
def ctx_no_mutations(mcp):
    _ctx = PluginContext(mcp, disable_mutating_tools=True)
    _set_plugin_context(_ctx)
    yield _ctx
    _set_plugin_context(None)


@pytest.fixture()
def mock_catalog():
    catalog = MagicMock()
    resp = MagicMock()
    resp.json.return_value = [{"RID": "1-AAAA", "Title": "Test Dataset"}]
    catalog.get.return_value = resp
    pb = MagicMock()
    mock_path = MagicMock()
    mock_path.filter.return_value = mock_path
    mock_path.update.return_value = [{"RID": "1-AAAA"}]
    pb.schemas.__getitem__.return_value.tables.__getitem__.return_value = mock_path
    catalog.getPathBuilder.return_value = pb
    return catalog
```

### Writing tests

Call `register(ctx)` in the test, then invoke tools directly via
`ctx._mcp.tools["tool_name"](...)`:

```python
# tests/test_my_plugin.py
import json
import pytest
from unittest.mock import patch
from deriva_mcp_core.context import set_current_credential
from my_package.mcp_plugin import register


async def test_read_tool(ctx, mock_catalog):
    set_current_credential({"bearer-token": "test-token"})
    register(ctx)
    with patch("deriva_mcp_core.get_catalog", return_value=mock_catalog):
        result = json.loads(
            await ctx._mcp.tools["my_read_tool"]("host.example.org", "1", "public")
        )
    assert "rows" in result
    assert result["rows"][0]["RID"] == "1-AAAA"


async def test_write_tool(ctx, mock_catalog):
    set_current_credential({"bearer-token": "test-token"})
    register(ctx)
    with patch("deriva_mcp_core.get_catalog", return_value=mock_catalog):
        result = json.loads(
            await ctx._mcp.tools["my_write_tool"](
                "host.example.org", "1", "public", "1-AAAA", "new description"
            )
        )
    assert result["updated"] is True


async def test_write_tool_blocked_by_kill_switch(ctx_no_mutations, mock_catalog):
    register(ctx_no_mutations)
    with patch("deriva_mcp_core.get_catalog", return_value=mock_catalog):
        result = json.loads(
            await ctx_no_mutations._mcp.tools["my_write_tool"](
                "host.example.org", "1", "public", "1-AAAA", "blocked"
            )
        )
    assert "error" in result
    mock_catalog.getPathBuilder.assert_not_called()


def test_mutates_required(ctx):
    """Omitting mutates= raises TypeError at registration time."""
    with pytest.raises(TypeError, match="mutates="):
        @ctx.tool()
        async def bad_tool() -> str:
            return ""
```

### Import patterns in tools

Import `get_catalog`, `deriva_call`, and other context functions **inside the tool
function body**, not at the top of `register()`. Outer-scope imports are captured
as closures at registration time -- before any test patch is applied. Inner imports
resolve at call time and pick up patched values correctly:

```python
def register(ctx: PluginContext) -> None:
    @ctx.tool(mutates=False)
    async def my_tool(hostname: str, catalog_id: str) -> str:
        # Import here, not at the top of register()
        from deriva_mcp_core import deriva_call, get_catalog
        with deriva_call():
            catalog = get_catalog(hostname, catalog_id)
            ...
```

This is the correct pattern for production code as well -- it avoids holding
stale module references when the server initializes before credentials are set.

### Testing lifecycle hooks

```python
import asyncio
from unittest.mock import MagicMock
from deriva_mcp_core.plugin.api import fire_catalog_connect


async def test_catalog_connect_hook_fires(ctx):
    index_fn = MagicMock()
    register(ctx, index_fn=index_fn)  # pass mock into register if needed

    fire_catalog_connect("host.example.org", "1", "deadbeef1234", {"schemas": {}})
    await asyncio.sleep(0)  # allow fire-and-forget task to run

    index_fn.assert_called_once_with("host.example.org", "1")


async def test_hook_exception_is_suppressed(ctx):
    async def _bad_hook(hostname, catalog_id, schema_hash, schema_json):
        raise RuntimeError("indexing failed")

    ctx.on_catalog_connect(_bad_hook)
    fire_catalog_connect("h", "1", "hash", {})
    await asyncio.sleep(0)  # must not raise
```