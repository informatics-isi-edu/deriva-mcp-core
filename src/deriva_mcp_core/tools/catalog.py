from __future__ import annotations

"""Schema introspection and catalog administration tools for DERIVA catalogs.

Provides MCP tools for browsing DERIVA catalog structure and server-level
catalog management operations.

Introspection tools (mutates=False):
    get_catalog_info            -- Catalog metadata: schema names and table counts
    list_schemas                -- Schema names within a catalog
    get_schema                  -- Tables and columns for a schema
    get_table                   -- Full definition of a single table
    cite                        -- Generate a Chaise citation URL for a catalog entity
    resolve_snaptime            -- Convert a human-readable date to an ERMrest snaptime string
    get_catalog_history_bounds  -- Return earliest/latest snapshot IDs for a catalog

Snapshot access: all introspection tools accept catalog_id as either a bare ID
("1") or a compound ID@snaptime string ("1@2X1234ABCD..."). Use resolve_snaptime
to convert a human-readable date to the snaptime component, and
get_catalog_history_bounds to discover what time range is available.

Administration tools (mutates=True):
    create_catalog        -- Create a new empty ERMrest catalog
    delete_catalog        -- Permanently delete a catalog
    clone_catalog         -- Clone a catalog into a new or existing destination
    create_catalog_alias  -- Create an ERMrest catalog alias
    update_catalog_alias  -- Update alias target or owner ACL
    delete_catalog_alias  -- Delete an alias (not the underlying catalog)
"""

import asyncio
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

import datetime

from dateutil import parser as _dateutil_parser
from deriva.core import DerivaServer
from deriva.core.ermrest_model import (
    datetime_to_snaptime as _datetime_to_snaptime,
    nochange as _nochange,
    snaptime_to_datetime as _snaptime_to_datetime,
    snaptime_to_timestamptz as _snaptime_to_timestamptz,
)

from . import fmt_exc
from ..context import (
    _remap,
    _set_catalog_access_fn,
    deriva_call,
    get_catalog,
    get_request_credential,
    get_request_user_id,
    resolve_user_identity,
)
from ..plugin.api import fire_catalog_connect
from ..tasks.manager import get_task_manager
from ..telemetry import audit_event

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)

# System-managed schemas hidden from user-visible listings
_SYSTEM_SCHEMAS: frozenset[str] = frozenset({"_ermrest", "_acl_admin"})

# Uppercase letters that appear in Crockford base32 but never in ISO date strings.
# A valid ERMrest snaptime (e.g. "2TA-YA2D-ZDWY") always contains at least one
# of these characters. Plain date strings ("2022-05-12") contain only digits and
# hyphens, so the presence of any of these letters distinguishes the two formats.
_CROCKFORD_LETTERS: frozenset[str] = frozenset("ABCDEFGHJKMNPQRSTVWXYZ")


def _looks_like_snaptime(value: str) -> bool:
    """Return True only if value plausibly encodes an ERMrest snaptime.

    ERMrest snaptimes are Crockford base32-encoded microsecond timestamps and
    always contain at least one uppercase letter from the Crockford alphabet.
    ISO date strings ("2022-05-12") and pure-numeric strings contain none of
    these letters, so this check reliably distinguishes the two formats.
    """
    return any(c in _CROCKFORD_LETTERS for c in value)


def _compute_schema_hash(schema_json: dict) -> str:
    return hashlib.sha256(json.dumps(schema_json, sort_keys=True).encode()).hexdigest()


def _col_summary(col: dict) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": col["name"],
        "type": col.get("type", {}).get("typename", "unknown"),
        "nullok": col.get("nullok", True),
    }
    comment = col.get("comment")
    if comment:
        info["comment"] = comment
    return info


def _fk_summary(fk: dict) -> dict[str, Any]:
    fk_cols = [c["column_name"] for c in fk.get("foreign_key_columns", [])]
    ref_cols = fk.get("referenced_columns", [])
    ref_table = f"{ref_cols[0]['schema_name']}:{ref_cols[0]['table_name']}" if ref_cols else None
    return {
        "columns": fk_cols,
        "references": ref_table,
        "referenced_columns": [c["column_name"] for c in ref_cols],
    }


def _fetch_schema(hostname: str, catalog_id: str, user_id: str) -> dict:
    """Fetch full schema JSON using the current credential, compute hash, fire hooks.

    The schema is fetched with the requesting user's derived token so the
    response reflects their actual ACL view -- tables and columns they cannot
    see are absent from the response. The hash of that response is the
    visibility-class key used by the RAG index.
    """
    # Pre-claim the slot so _on_catalog_access (triggered by get_catalog below)
    # does not schedule a redundant background _fetch_schema for the same key.
    _connected_user_catalogs.add((_remap(hostname), catalog_id, user_id))
    with deriva_call():
        catalog = get_catalog(hostname, catalog_id)
        # Note: deriva-py catalog.get() is a synchronous requests call.
        schema_json = catalog.get("/schema").json()
    schema_hash = _compute_schema_hash(schema_json)
    fire_catalog_connect(hostname, catalog_id, schema_hash, schema_json)
    return schema_json


# Per-server-lifetime set of (internal_hostname, catalog_id, user_id) triples
# whose on_catalog_connect hooks have already been fired for that user's
# ACL view. Keying by user_id ensures each distinct identity fetches its own
# /schema so the visibility-class hash accurately reflects what that user can
# see. Two users with identical effective ACLs will produce the same hash and
# share one vector-store entry; a restricted user gets a separate entry.
_connected_user_catalogs: set[tuple[str, str, str]] = set()
_connect_tasks: set[asyncio.Task] = set()


def _on_catalog_access(hostname: str, catalog_id: str) -> None:
    """Callback registered with context.py; fired by get_catalog() on every call.

    Resolves the current user identity (contextvar in HTTP mode; GET /authn/session
    in stdio mode) and schedules a per-user background schema fetch the first time
    a given (catalog, user) pair is seen. Subsequent calls are a set lookup.
    """
    user_id = resolve_user_identity(hostname)
    key = (hostname, catalog_id, user_id)
    if key in _connected_user_catalogs:
        return
    _connected_user_catalogs.add(key)

    async def _do() -> None:
        try:
            _fetch_schema(hostname, catalog_id, user_id)
        except Exception:
            logger.debug(
                "Background on_catalog_connect failed for %s/%s (user %s)",
                hostname, catalog_id, user_id, exc_info=True,
            )
            _connected_user_catalogs.discard(key)

    try:
        task = asyncio.get_running_loop().create_task(_do())
        _connect_tasks.add(task)
        task.add_done_callback(_connect_tasks.discard)
    except RuntimeError:
        pass  # no running event loop (e.g., during import-time tests)


def _parse_snaptime(value: str) -> str:
    """Convert a human-readable date/time string or existing snaptime to ERMrest snaptime format.

    Accepts:
        - An existing ERMrest snaptime string (Crockford base32) -- returned as-is.
        - An ISO-8601 datetime string (e.g. "2024-01-15T12:00:00Z").
        - Any date/time format parseable by dateutil (e.g. "Jan 15 2024", "yesterday").

    Returns the ERMrest snaptime string.
    Raises ValueError if the input cannot be parsed.
    """
    # Try to interpret as an existing snaptime first.
    # Only attempt Crockford decode when the string contains uppercase Crockford
    # letters -- ISO date strings like "2022-05-12" contain only digits and hyphens
    # and the Crockford decoder would silently produce a garbage ~1970 timestamp.
    if _looks_like_snaptime(value):
        try:
            _snaptime_to_datetime(value)
            return value
        except Exception:
            pass

    # Parse as a human-readable date/time string
    try:
        dt = _dateutil_parser.parse(value)
    except Exception as exc:
        raise ValueError(f"Cannot parse date/time {value!r}: {exc}") from exc

    # Ensure timezone-aware (assume UTC if no timezone given)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)

    return _datetime_to_snaptime(dt)


def register(ctx: PluginContext) -> None:
    """Register schema introspection tools with the MCP server."""
    _set_catalog_access_fn(_on_catalog_access)

    @ctx.tool(mutates=False)
    async def get_catalog_info(hostname: str, catalog_id: str) -> str:
        """Get metadata for a DERIVA catalog: schema names and table counts.

        Fetches the full catalog schema and fires the on_catalog_connect lifecycle
        hook (used by the RAG subsystem to index the schema).

        Args:
            hostname: Hostname of the DERIVA server (e.g. "deriva.example.org").
            catalog_id: Catalog ID, alias, or compound ID@snaptime for a historical
                snapshot (e.g. "1", "my-catalog", "1@2TA-YA2D-ZDWY"). The snaptime
                component must be a Crockford base32 string (with dashes) -- never
                a plain date. Call resolve_snaptime first to convert a date.
        """
        try:
            schema_json = _fetch_schema(hostname, catalog_id, resolve_user_identity(hostname))
            schemas = [
                {
                    "schema": name,
                    "tables": len(s.get("tables", {})),
                    "comment": s.get("comment"),
                }
                for name, s in schema_json.get("schemas", {}).items()
                if name not in _SYSTEM_SCHEMAS
            ]
            return json.dumps(
                {
                    "hostname": hostname,
                    "catalog_id": catalog_id,
                    "schemas": schemas,
                }
            )
        except Exception as exc:
            logger.error("get_catalog_info failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def list_schemas(hostname: str, catalog_id: str) -> str:
        """List schema names in a DERIVA catalog.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID, alias, or compound ID@snaptime for a historical
                snapshot. The snaptime must be a Crockford base32 string -- never
                a plain date. Call resolve_snaptime first to convert a date.
        """
        try:
            schema_json = _fetch_schema(hostname, catalog_id, resolve_user_identity(hostname))
            names = [n for n in schema_json.get("schemas", {}) if n not in _SYSTEM_SCHEMAS]
            return json.dumps({"schemas": names})
        except Exception as exc:
            logger.error("list_schemas failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def get_schema(hostname: str, catalog_id: str, schema: str) -> str:
        """Get tables and columns for a schema.

        Returns every table in the schema with its comment and all column
        names, types, and nullability.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID, alias, or compound ID@snaptime for a historical
                snapshot. The snaptime must be a Crockford base32 string -- never
                a plain date. Call resolve_snaptime first to convert a date.
            schema: Schema name (e.g. "public" or "isa").
        """
        try:
            schema_json = _fetch_schema(hostname, catalog_id, resolve_user_identity(hostname))
            schema_doc = schema_json.get("schemas", {}).get(schema)
            if schema_doc is None:
                return json.dumps({"error": f"Schema not found: {schema!r}"})

            tables = [
                {
                    "table": tname,
                    "comment": tdef.get("comment"),
                    "columns": [_col_summary(c) for c in tdef.get("column_definitions", [])],
                }
                for tname, tdef in schema_doc.get("tables", {}).items()
            ]
            return json.dumps(
                {
                    "schema": schema,
                    "comment": schema_doc.get("comment"),
                    "tables": tables,
                }
            )
        except Exception as exc:
            logger.error("get_schema failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def get_table(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
    ) -> str:
        """Get the full definition of a table: columns, keys, and foreign keys.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID, alias, or compound ID@snaptime for a historical
                snapshot. The snaptime must be a Crockford base32 string -- never
                a plain date. Call resolve_snaptime first to convert a date.
            schema: Schema name.
            table: Table name.
        """
        try:
            schema_json = _fetch_schema(hostname, catalog_id, resolve_user_identity(hostname))
            schema_doc = schema_json.get("schemas", {}).get(schema)
            if schema_doc is None:
                return json.dumps({"error": f"Schema not found: {schema!r}"})
            tdef = schema_doc.get("tables", {}).get(table)
            if tdef is None:
                return json.dumps({"error": f"Table not found: {schema!r}:{table!r}"})

            keys = [{"columns": k.get("unique_columns", [])} for k in tdef.get("keys", [])]
            return json.dumps(
                {
                    "schema": schema,
                    "table": table,
                    "comment": tdef.get("comment"),
                    "kind": tdef.get("kind", "table"),
                    "columns": [_col_summary(c) for c in tdef.get("column_definitions", [])],
                    "keys": keys,
                    "foreign_keys": [_fk_summary(fk) for fk in tdef.get("foreign_keys", [])],
                }
            )
        except Exception as exc:
            logger.error("get_table failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def resolve_snaptime(
        date_string: str,
        hostname: str | None = None,
        catalog_id: str | None = None,
    ) -> str:
        """Resolve a human-readable date/time to an ERMrest snaptime string.

        See CATALOG TOOL GUIDE for snaptime format details and catalog
        management patterns.

        ERMrest snaptimes are Crockford base32 timestamps (e.g. "2TA-YA2D-ZDWY"),
        not ISO dates. Always call this tool before constructing ID@snaptime
        compound identifiers.

        Provide hostname + catalog_id for a server-confirmed canonical snaptime.
        Without them, the tool returns a probe snaptime only.

        Args:
            date_string: ISO date, common date string, relative expression
                ("yesterday"), or existing Crockford base32 snaptime.
            hostname: DERIVA server hostname (enables canonical lookup).
            catalog_id: Catalog ID or alias (enables canonical lookup).
        """
        try:
            probe_snaptime = _parse_snaptime(date_string)

            if hostname and catalog_id:
                # Server round-trip: GET /ermrest/catalog/{id}@{probe} returns
                # the catalog metadata including the canonical snaptime that covers
                # the requested moment.
                with deriva_call():
                    catalog = get_catalog(hostname, f"{catalog_id}@{probe_snaptime}")
                    info = catalog.get("/").json()
                canonical_snaptime = info["snaptime"]
                timestamp = _snaptime_to_timestamptz(canonical_snaptime)
                return json.dumps({
                    "snaptime": canonical_snaptime,
                    "timestamp": timestamp,
                    "canonical": True,
                    "compound_id": f"{catalog_id}@{canonical_snaptime}",
                })
            else:
                timestamp = _snaptime_to_timestamptz(probe_snaptime)
                return json.dumps({
                    "snaptime": probe_snaptime,
                    "timestamp": timestamp,
                    "canonical": False,
                    "note": "Provide hostname and catalog_id for a server-confirmed canonical snaptime.",
                })
        except Exception as exc:
            logger.error("resolve_snaptime failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def get_catalog_history_bounds(hostname: str, catalog_id: str) -> str:
        """Return the earliest and latest snapshot identifiers for a catalog.

        See CATALOG TOOL GUIDE for snaptime format details.

        Use this to discover the available history range before calling
        resolve_snaptime.

        Args:
            hostname: DERIVA server hostname.
            catalog_id: Catalog ID or alias (bare ID only, not ID@snaptime).
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                info = catalog.get("/history/,").json()
            snaprange = info.get("snaprange") or []
            earliest = snaprange[0] if len(snaprange) > 0 else None
            latest = snaprange[1] if len(snaprange) > 1 else None
            return json.dumps({
                "earliest_snaptime": earliest,
                "earliest_timestamp": _snaptime_to_timestamptz(earliest) if earliest else None,
                "latest_snaptime": latest,
                "latest_timestamp": _snaptime_to_timestamptz(latest) if latest else None,
                "amendver": info.get("amendver"),
            })
        except Exception as exc:
            logger.error("get_catalog_history_bounds failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=True)
    async def create_catalog(
        hostname: str,
        initial_schema: str | None = None,
        initial_schema_comment: str = "",
        name: str | None = None,
        description: str | None = None,
        catalog_id: str | None = None,
        owner: list | None = None,
        is_persistent: bool | None = None,
    ) -> str:
        """Create a new empty ERMrest catalog on a DERIVA server.

        Creates a catalog with the ERMrest system schema only. No DerivaML
        schema is initialized; further schema setup is the caller's responsibility.
        If initial_schema is provided, that schema is created inside the new catalog.

        Args:
            hostname: Hostname of the DERIVA server.
            initial_schema: Optional name for a schema to create inside the new catalog.
            initial_schema_comment: Description for the initial schema (optional).
            name: Human-readable name for the catalog (optional).
            description: Description of the catalog (optional).
            catalog_id: Desired catalog ID string (optional; server assigns one if omitted).
            owner: Initial owner ACL as a list of strings (optional).
            is_persistent: Set catalog persistence flag (optional; server default if omitted).
        """
        try:
            with deriva_call():
                internal = _remap(hostname)
                server = DerivaServer("https", internal, credentials=get_request_credential())
                catalog = server.create_ermrest_catalog(
                    id=catalog_id,
                    owner=owner,
                    name=name,
                    description=description,
                    is_persistent=is_persistent,
                )
                catalog_id = str(catalog.catalog_id)
                created_schema = None
                if initial_schema:
                    model = catalog.getCatalogModel()
                    model.create_schema(
                        {"schema_name": initial_schema, "comment": initial_schema_comment}
                    )
                    created_schema = initial_schema
            audit_event(
                "catalog_create",
                hostname=hostname,
                catalog_id=catalog_id,
                name=name,
                initial_schema=initial_schema,
            )
            result: dict = {"status": "created", "hostname": hostname, "catalog_id": catalog_id}
            if created_schema:
                result["initial_schema"] = created_schema
            return json.dumps(result)
        except Exception as exc:
            logger.error("create_catalog failed: %s", exc)
            audit_event(
                "catalog_create_failed",
                hostname=hostname,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def delete_catalog(
        hostname: str,
        catalog_id: str,
    ) -> str:
        """Permanently delete an ERMrest catalog.

        This operation is irreversible. All data in the catalog is destroyed.
        ERMrest may refuse if the catalog has active aliases pointing to it.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID to delete.
        """
        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                catalog.delete_ermrest_catalog(really=True)
            audit_event(
                "catalog_delete",
                hostname=hostname,
                catalog_id=catalog_id,
            )
            return json.dumps({"status": "deleted", "hostname": hostname, "catalog_id": catalog_id})
        except Exception as exc:
            logger.error("delete_catalog failed: %s", exc)
            audit_event(
                "catalog_delete_failed",
                hostname=hostname,
                catalog_id=catalog_id,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def clone_catalog(
        hostname: str,
        source_catalog_id: str,
        dest_catalog_id: str | None = None,
        copy_data: bool = True,
        copy_annotations: bool = True,
        copy_policy: bool = True,
        exclude_schemas: list[str] | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> str:
        """Clone an ERMrest catalog into a new or existing destination catalog.

        Uses the ERMrest idempotent clone protocol: each table is annotated with
        a clone-state marker so an interrupted clone can be safely resumed by
        calling this tool again with the same dest_catalog_id.

        When dest_catalog_id is omitted a new catalog is provisioned automatically.
        When dest_catalog_id is provided the clone is applied on top of any
        existing content (useful for resuming a partial clone).

        Args:
            hostname: Hostname of the DERIVA server (source and destination must
                be on the same server).
            source_catalog_id: Catalog ID to clone from.
            dest_catalog_id: Destination catalog ID. If omitted, a new catalog
                is created.
            copy_data: Copy table row data (default True).
            copy_annotations: Copy catalog and table annotations (default True).
            copy_policy: Copy ACL policies (default True).
            exclude_schemas: Schema names to skip during cloning (optional).
            name: Human-readable name for the destination catalog (optional;
                defaults to "Clone of {source_catalog_id}").
            description: Description for the destination catalog (optional).
        """
        try:
            with deriva_call():
                src_catalog = get_catalog(hostname, source_catalog_id)
                dst_properties: dict = {}
                if name:
                    dst_properties["name"] = name
                if description:
                    dst_properties["description"] = description
                if dest_catalog_id:
                    dst_catalog = get_catalog(hostname, dest_catalog_id)
                else:
                    dst_catalog = None
                result_catalog = src_catalog.clone_catalog(
                    dst_catalog=dst_catalog,
                    copy_data=copy_data,
                    copy_annotations=copy_annotations,
                    copy_policy=copy_policy,
                    truncate_after=True,
                    exclude_schemas=exclude_schemas or [],
                    dst_properties=dst_properties or None,
                )
                new_catalog_id = str(result_catalog.catalog_id)
            audit_event(
                "catalog_clone",
                hostname=hostname,
                source_catalog_id=source_catalog_id,
                dest_catalog_id=new_catalog_id,
            )
            return json.dumps({
                "status": "cloned",
                "hostname": hostname,
                "source_catalog_id": source_catalog_id,
                "dest_catalog_id": new_catalog_id,
            })
        except Exception as exc:
            logger.error("clone_catalog failed: %s", exc)
            audit_event(
                "catalog_clone_failed",
                hostname=hostname,
                source_catalog_id=source_catalog_id,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def clone_catalog_async(
        hostname: str,
        source_catalog_id: str,
        dest_catalog_id: str | None = None,
        copy_data: bool = True,
        copy_annotations: bool = True,
        copy_policy: bool = True,
        exclude_schemas: list[str] | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> str:
        """Submit a catalog clone as a background task. Returns task_id immediately.

        Use get_task_status(task_id) to poll for completion. The clone runs in a
        background thread so it does not block the server. For small catalogs,
        prefer clone_catalog (synchronous) to avoid the polling overhead.

        Args:
            hostname: Hostname of the DERIVA server.
            source_catalog_id: Catalog ID to clone from.
            dest_catalog_id: Destination catalog ID. If omitted, a new catalog is created.
            copy_data: Copy table row data (default True).
            copy_annotations: Copy catalog and table annotations (default True).
            copy_policy: Copy ACL policies (default True).
            exclude_schemas: Schema names to skip during cloning (optional).
            name: Human-readable name for the destination catalog (optional).
            description: Description for the destination catalog (optional).
        """
        internal = _remap(hostname)
        # Capture principal at submission time for audit events inside the task.
        principal = get_request_user_id()
        # Mutable ref so the inner coroutine can read the task_id once assigned.
        # Safe under asyncio cooperative scheduling: task_id_ref is populated
        # before the event loop yields control to the background task.
        task_id_ref: list[str] = []

        async def _do_clone() -> dict:
            task_id = task_id_ref[0]
            mgr = get_task_manager()
            cred = await mgr.get_credential(task_id)

            def _run_sync() -> str:
                server = DerivaServer("https", internal, credentials=cred)
                src_catalog = server.connect_ermrest(source_catalog_id)
                dst_properties: dict = {}
                if name:
                    dst_properties["name"] = name
                if description:
                    dst_properties["description"] = description
                if dest_catalog_id:
                    dst_catalog = server.connect_ermrest(dest_catalog_id)
                else:
                    dst_catalog = None
                result_catalog = src_catalog.clone_catalog(
                    dst_catalog=dst_catalog,
                    copy_data=copy_data,
                    copy_annotations=copy_annotations,
                    copy_policy=copy_policy,
                    truncate_after=True,
                    exclude_schemas=exclude_schemas or [],
                    dst_properties=dst_properties or None,
                )
                return str(result_catalog.catalog_id)

            new_catalog_id = await asyncio.to_thread(_run_sync)
            audit_event(
                "catalog_clone",
                hostname=hostname,
                source_catalog_id=source_catalog_id,
                dest_catalog_id=new_catalog_id,
                principal=principal,
            )
            return {
                "status": "cloned",
                "hostname": hostname,
                "source_catalog_id": source_catalog_id,
                "dest_catalog_id": new_catalog_id,
            }

        try:
            task_id = ctx.submit_task(
                _do_clone(),
                name=f"clone_catalog {source_catalog_id}",
            )
            task_id_ref.append(task_id)
        except Exception as exc:
            logger.error("clone_catalog_async failed to submit: %s", exc)
            return json.dumps({"error": fmt_exc(exc)})
        audit_event(
            "catalog_clone_async_submitted",
            hostname=hostname,
            source_catalog_id=source_catalog_id,
            task_id=task_id,
        )
        return json.dumps({"task_id": task_id, "status": "submitted"})

    @ctx.tool(mutates=True)
    async def create_catalog_alias(
        hostname: str,
        alias_name: str,
        alias_target: str,
        name: str | None = None,
        description: str | None = None,
        owner: list | None = None,
    ) -> str:
        """Create an ERMrest catalog alias.

        An alias is a stable short name that can point to different catalog IDs
        over time. Use update_catalog_alias to retarget the alias.

        Args:
            hostname: Hostname of the DERIVA server.
            alias_name: Alias identifier (e.g. "my-project-live").
            alias_target: Catalog ID the alias points to.
            name: Human-readable name for the alias (optional).
            description: Description for the alias (optional).
            owner: Initial owner ACL as a list of strings (optional).
        """
        try:
            with deriva_call():
                internal = _remap(hostname)
                server = DerivaServer("https", internal, credentials=get_request_credential())
                server.create_ermrest_alias(
                    alias_name,
                    owner=owner,
                    alias_target=alias_target,
                    name=name or "",
                    description=description or "",
                )
            audit_event(
                "catalog_alias_create",
                hostname=hostname,
                alias_name=alias_name,
                alias_target=alias_target,
            )
            return json.dumps({
                "status": "created",
                "hostname": hostname,
                "alias_name": alias_name,
                "alias_target": alias_target,
            })
        except Exception as exc:
            logger.error("create_catalog_alias failed: %s", exc)
            audit_event(
                "catalog_alias_create_failed",
                hostname=hostname,
                alias_name=alias_name,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def update_catalog_alias(
        hostname: str,
        alias_name: str,
        alias_target: str | None = None,
        owner: list | None = None,
    ) -> str:
        """Update an ERMrest catalog alias target or owner ACL.

        At least one of alias_target or owner must be provided. Omitting
        a field leaves it unchanged.

        Args:
            hostname: Hostname of the DERIVA server.
            alias_name: Alias identifier to update.
            alias_target: New catalog ID for the alias to point to (optional).
            owner: New owner ACL list (optional).
        """
        if alias_target is None and owner is None:
            return json.dumps({"error": "at least one of alias_target or owner must be provided"})
        try:
            with deriva_call():
                internal = _remap(hostname)
                server = DerivaServer("https", internal, credentials=get_request_credential())
                alias = server.connect_ermrest_alias(alias_name)
                alias.update(  # type: ignore
                    owner=owner if owner is not None else _nochange,
                    alias_target=alias_target if alias_target is not None else _nochange,
                )
            audit_event(
                "catalog_alias_update",
                hostname=hostname,
                alias_name=alias_name,
            )
            return json.dumps({
                "status": "updated",
                "hostname": hostname,
                "alias_name": alias_name,
            })
        except Exception as exc:
            logger.error("update_catalog_alias failed: %s", exc)
            audit_event(
                "catalog_alias_update_failed",
                hostname=hostname,
                alias_name=alias_name,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=True)
    async def delete_catalog_alias(
        hostname: str,
        alias_name: str,
    ) -> str:
        """Delete an ERMrest catalog alias.

        Deletes the alias only; the underlying catalog is not affected.

        Args:
            hostname: Hostname of the DERIVA server.
            alias_name: Alias identifier to delete.
        """
        try:
            with deriva_call():
                internal = _remap(hostname)
                server = DerivaServer("https", internal, credentials=get_request_credential())
                alias = server.connect_ermrest_alias(alias_name)
                alias.delete_ermrest_alias(really=True)
            audit_event(
                "catalog_alias_delete",
                hostname=hostname,
                alias_name=alias_name,
            )
            return json.dumps({
                "status": "deleted",
                "hostname": hostname,
                "alias_name": alias_name,
            })
        except Exception as exc:
            logger.error("delete_catalog_alias failed: %s", exc)
            audit_event(
                "catalog_alias_delete_failed",
                hostname=hostname,
                alias_name=alias_name,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": fmt_exc(exc)})

    @ctx.tool(mutates=False)
    async def cite(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
        rid: str,
        current: bool = False,
    ) -> str:
        """Generate a Chaise citation URL for a catalog entity.

        When current=False (default), the URL includes the catalog snapshot
        timestamp at the moment of the call, making it a versioned permanent
        citation suitable for publications. When current=True, the URL links
        to the live record without a snapshot.

        URL format (versioned):
            https://{hostname}/chaise/record/#{catalog_id}@{snaptime}/{schema}:{table}/RID={rid}

        URL format (current):
            https://{hostname}/chaise/record/#{catalog_id}/{schema}:{table}/RID={rid}

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name containing the entity table.
            table: Table name.
            rid: RID of the entity to cite (e.g. "2A-1234").
            current: If True, omit the snapshot timestamp (live URL).
        """
        try:
            if current:
                url = (
                    f"https://{hostname}/chaise/record/"
                    f"#{catalog_id}/{schema}:{table}/RID={rid}"
                )
            else:
                with deriva_call():
                    catalog = get_catalog(hostname, catalog_id)
                    snaptime = catalog.latest_snapshot().snaptime
                url = (
                    f"https://{hostname}/chaise/record/"
                    f"#{catalog_id}@{snaptime}/{schema}:{table}/RID={rid}"
                )
            return json.dumps({
                "url": url,
                "is_snapshot": not current,
            })
        except Exception as exc:
            logger.error("cite failed: %s", exc)
            return json.dumps({"error": fmt_exc(exc)})
