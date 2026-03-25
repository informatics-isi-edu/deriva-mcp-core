from __future__ import annotations

"""Hatrac object store tools for DERIVA.

Provides MCP tools for basic Hatrac namespace and object operations:
    list_namespace        -- List objects and namespaces under a path
    get_object_metadata   -- Retrieve object metadata (not content)
    create_namespace      -- Create a new Hatrac namespace
"""

import json
import logging
from typing import TYPE_CHECKING

from ..context import deriva_call, get_hatrac_store
from ..telemetry import audit_event

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)


def register(ctx: PluginContext) -> None:
    """Register Hatrac object store tools with the MCP server."""

    @ctx.tool(mutates=False)
    async def list_namespace(hostname: str, path: str) -> str:
        """List objects and sub-namespaces under a Hatrac namespace path.

        A trailing slash is added automatically if absent. Returns a JSON
        array of the names of objects and sub-namespaces at that path.

        Args:
            hostname: Hostname of the DERIVA server.
            path: Hatrac namespace path (e.g. "/hatrac/my/namespace").
        """
        try:
            with deriva_call():
                store = get_hatrac_store(hostname)
                # Hatrac namespace listing requires trailing slash
                ns_path = path.rstrip("/") + "/"
                contents = store.get(ns_path).json()
                return json.dumps(
                    {
                        "path": ns_path,
                        "contents": contents,
                    }
                )
        except Exception as exc:
            logger.error("list_namespace failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def get_object_metadata(hostname: str, path: str) -> str:
        """Retrieve metadata for a Hatrac object (does not fetch content).

        Returns the object's HTTP headers as a metadata dict, including
        content-type, content-length, and any Hatrac-specific headers.

        Args:
            hostname: Hostname of the DERIVA server.
            path: Hatrac object path (e.g. "/hatrac/my/namespace/file.txt").
        """
        try:
            with deriva_call():
                store = get_hatrac_store(hostname)
                resp = store.head(path)
                metadata = {k.lower(): v for k, v in resp.headers.items()}
                return json.dumps(
                    {
                        "path": path,
                        "metadata": metadata,
                    }
                )
        except Exception as exc:
            logger.error("get_object_metadata failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=True)
    async def create_namespace(hostname: str, path: str) -> str:
        """Create a new Hatrac namespace at the given path.

        The path should end with a slash; one is added automatically if absent.
        Creating a namespace that already exists is idempotent.

        Args:
            hostname: Hostname of the DERIVA server.
            path: Namespace path to create (e.g. "/hatrac/my/new/namespace").
        """
        try:
            with deriva_call():
                store = get_hatrac_store(hostname)
                ns_path = path.rstrip("/") + "/"
                store.put(ns_path, data=b"", headers={"Content-Type": "application/x-hatrac-namespace"})
            audit_event("hatrac_create_namespace", hostname=hostname, path=ns_path)
            return json.dumps(
                {
                    "status": "created",
                    "path": ns_path,
                }
            )
        except Exception as exc:
            logger.error("create_namespace failed: %s", exc)
            audit_event(
                "hatrac_create_namespace_failed",
                hostname=hostname,
                path=path,
                error_type=type(exc).__name__,
            )
            return json.dumps({"error": str(exc)})
