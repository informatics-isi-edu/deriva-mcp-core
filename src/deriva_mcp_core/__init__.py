"""deriva-mcp-core -- Reference implementation MCP server for the DERIVA platform.

Public API (import from deriva_mcp_core):

    get_deriva_server(hostname)
        Returns an authenticated DerivaServer for the current request context.
        Use to obtain ErmrestCatalog and other bindings:
            catalog = get_deriva_server(hostname).connect_ermrest(catalog_id)

    get_hatrac_store(hostname)
        Returns an authenticated HatracStore for the current request context.

    get_request_credential()
        Returns the per-request credential dict. Use when passing credentials to
        a higher-level API that constructs its own client:
            DerivaML(hostname, catalog_id, credential=get_request_credential())
        Distinct from deriva.core.get_credential() which reads from local disk.
"""

from __future__ import annotations

from deriva_mcp_core.context import get_deriva_server, get_hatrac_store, get_request_credential

__all__ = ["get_deriva_server", "get_hatrac_store", "get_request_credential"]
