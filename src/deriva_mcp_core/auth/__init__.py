from __future__ import annotations

"""Auth layer for deriva-mcp-core.

Submodules:
    introspect   -- Credenza POST /introspect client
    exchange     -- Credenza POST /token token_exchange client
    token_cache  -- Smart derived token cache (keyed by sub, per-sub async locking)
    verifier     -- CredenzaTokenVerifier (mcp TokenVerifier protocol, used by FastMCP)
"""

