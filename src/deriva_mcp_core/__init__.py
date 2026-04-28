from __future__ import annotations

"""deriva-mcp-core -- Reference implementation MCP server for the DERIVA platform.

Public API (import from deriva_mcp_core):

    get_catalog(hostname, catalog_id)
        Returns an authenticated ErmrestCatalog for the current request context.
        Use to fetch and mutate catalog data:
            catalog = get_catalog(hostname, catalog_id)
            rows = catalog.get("/entity/schema:table").json()

    get_hatrac_store(hostname)
        Returns an authenticated HatracStore for the current request context.

    get_credential(hostname)
        Returns the credential for the given hostname. Works in both HTTP and
        stdio mode. Use this in plugins that construct higher-level DERIVA
        clients directly:
            DerivaML(hostname, catalog_id, credential=get_credential(hostname))

    get_request_credential()
        HTTP mode only. Returns the per-request credential dict from the
        contextvar set by the auth verifier. Raises RuntimeError in stdio mode.
        Prefer get_credential(hostname) for new plugin code.

    get_request_user_id()
        Returns the user identity (iss/sub) for the current request context.
        In HTTP mode this is derived from Credenza token introspection.
        In stdio mode returns "stdio".

    deriva_call()
        Context manager for DERIVA tool calls. Catches downstream HTTP 401
        responses, evicts the stale derived token from the cache so the next
        request triggers a fresh exchange, then re-raises. Use in every tool
        handler that calls get_catalog() or get_hatrac_store()::

            try:
                with deriva_call():
                    catalog = get_catalog(hostname, catalog_id)
                    result = catalog.get(url).json()
                    return json.dumps({...})
            except Exception as exc:
                return json.dumps({"error": str(exc)})
"""


from .context import (
    deriva_call,
    get_catalog,
    get_credential,
    get_hatrac_store,
    get_request_credential,
    get_request_user_id,
    resolve_user_identity,
)

__all__ = [
    "deriva_call",
    "get_catalog",
    "get_credential",
    "get_hatrac_store",
    "get_request_credential",
    "get_request_user_id",
    "resolve_user_identity",
]