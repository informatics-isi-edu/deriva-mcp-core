"""FastMCP server factory and CLI entrypoint for deriva-mcp-core.

Transport modes:
    streamable-http -- Production. Auth middleware validates bearer tokens via Credenza.
    stdio           -- Local development only. Credential read from ~/.deriva/credential.json.

Usage:
    deriva-mcp-core                                          # stdio (default)
    deriva-mcp-core --transport streamable-http              # HTTP on 127.0.0.1:8000
    deriva-mcp-core --transport streamable-http --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

# TODO (Phase 3): implement create_server(), main(), CLI arg parsing
