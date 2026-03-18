"""Per-request credential context for deriva-mcp-core.

The auth middleware (HTTP mode) or server startup (stdio mode) sets the current
credential via set_current_credential() before any tool or resource handler runs.

Tool and resource handlers access DERIVA via the public functions exported from
this module. Handlers never touch tokens directly.

Public API:
    get_deriva_server(hostname)  -> DerivaServer   -- connection root for ERMREST operations
    get_hatrac_store(hostname)  -> HatracStore    -- connection root for Hatrac operations
    get_request_credential() -> dict        -- credential dict for higher-level APIs
                                               (e.g. DerivaML)
"""

from __future__ import annotations

import contextvars

# Per-request credential dict. Format matches what DerivaBinding accepts:
#   {"bearer-token": "<token>"}        -- HTTP mode (derived Credenza token)
#   {"cookie": "webauthn=<token>"}     -- stdio mode (local credential)
_current_credential: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_credential", default=None
)


def set_current_credential(credential: dict) -> None:
    """Set the credential for the current request context.

    Called by the auth verifier (HTTP) or server startup (stdio).
    Not intended for use in tool or resource handlers.
    """
    _current_credential.set(credential)


def get_request_credential() -> dict:
    """Return the per-request credential for the current async context.

    Use this when passing credentials to a higher-level API that constructs its
    own client, e.g.: DerivaML(hostname, catalog_id, credential=get_request_credential())

    Distinct from deriva.core.get_credential(), which reads credentials from the
    local filesystem. This function reads from the per-request contextvar set by
    the auth verifier (HTTP) or server startup (stdio).

    Raises RuntimeError if called outside a tool or resource handler context.
    """
    credential = _current_credential.get()
    if credential is None:
        raise RuntimeError(
            "No credential in current request context. "
            "This function must be called from within a tool or resource handler."
        )
    return credential


def get_deriva_server(hostname: str):
    """Return an authenticated DerivaServer for the current request context.

    DerivaServer is the connection root for ERMREST operations. Use it to obtain
    catalog and alias bindings:

        catalog = get_deriva_server(hostname).connect_ermrest(catalog_id)
        alias   = get_deriva_server(hostname).connect_ermrest_alias(alias_id)

    Args:
        hostname: Hostname of the DERIVA server (e.g. "deriva.example.org").

    Returns:
        A DerivaServer authenticated with the current request credential.

    Raises:
        RuntimeError: If called outside a tool or resource handler context.
    """
    from deriva.core import DerivaServer

    return DerivaServer("https", hostname, credentials=get_request_credential())


def get_hatrac_store(hostname: str):
    """Return an authenticated HatracStore for the current request context.

    HatracStore is the connection root for Hatrac object store operations.
    It is a separate binding from DerivaServer and operates on the /hatrac/ URL base.

    Args:
        hostname: Hostname of the DERIVA server (e.g. "deriva.example.org").

    Returns:
        A HatracStore authenticated with the current request credential.

    Raises:
        RuntimeError: If called outside a tool or resource handler context.
    """
    from deriva.core import HatracStore

    return HatracStore("https", hostname, credentials=get_request_credential())
