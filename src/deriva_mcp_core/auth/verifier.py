"""Credenza token verifier for FastMCP HTTP transport.

Implements the mcp.server.auth.provider.TokenVerifier protocol. Passed to
FastMCP(token_verifier=CredenzaTokenVerifier(...)) so that FastMCP's built-in
auth layer handles bearer token extraction and 401 responses automatically.

On each request:
    1. FastMCP extracts the Authorization: Bearer <token> header and calls verify_token()
    2. verify_token() introspects the token via Credenza POST /introspect
    3. Validates the token is active and audience contains DERIVA_MCP_SERVER_RESOURCE
    4. Obtains a derived DERIVA-scoped token via the smart token cache (exchange on miss)
    5. Sets the per-request credential contextvar for use by tool/resource handlers
    6. Returns an AccessToken; returning None causes FastMCP to issue a 401
"""

from __future__ import annotations

# TODO (Phase 3): implement CredenzaTokenVerifier
