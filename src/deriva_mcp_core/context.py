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

import contextvars
from collections.abc import Callable

from deriva.core import DerivaServer, HatracStore

# Per-request credential dict. Format matches what DerivaBinding accepts:
#   {"bearer-token": "<token>"}        -- HTTP mode (derived Credenza token)
#   {"cookie": "webauthn=<token>"}     -- stdio mode (local credential)
_current_credential: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_credential", default=None
)

# Per-request user identity (sub from token introspection in HTTP mode).
# None when not yet authenticated (pre-auth or stdio transport).
_current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user_id", default=None
)


def set_current_credential(credential: dict) -> None:
    """Set the credential for the current request context.

    Called by the auth verifier (HTTP) or server startup (stdio).
    Not intended for use in tool or resource handlers.
    """
    _current_credential.set(credential)


def set_current_user_id(user_id: str) -> None:
    """Set the user identity for the current request context.

    Called by the auth verifier (HTTP) with the sub from token introspection.
    Not intended for use in tool or resource handlers.
    """
    _current_user_id.set(user_id)


def get_request_user_id_optional() -> str | None:
    """Return the user identity for the current async context, or None if not set.

    Returns None in HTTP mode before authentication completes and in stdio mode
    (where no token sub is available). Use this when the absence of an identity
    is meaningful, e.g. audit logging pre-auth events.
    """
    return _current_user_id.get()


def get_request_user_id() -> str:
    """Return the user identity for the current async context.

    In HTTP mode this is the sub from token introspection. In stdio mode it
    returns "stdio" (single-user, no token sub available).

    Use this to scope per-user data indexes in the RAG subsystem.
    """
    uid = _current_user_id.get()
    return uid if uid is not None else "stdio"


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


# Active credential resolver. In HTTP mode (default) this wraps the per-request
# contextvar. In stdio mode server.py replaces it with deriva.core.get_credential
# at startup so each call resolves credentials from ~/.deriva/credential.json.
def _contextvar_credential(_hostname: str) -> dict:
    return get_request_credential()


_get_credential_fn: Callable[[str], dict] = _contextvar_credential

# Hostname remap table. Maps external hostnames to internal network aliases so
# that tool calls using a public hostname (e.g. "localhost") are routed to the
# correct endpoint from inside the container (e.g. "deriva").
_hostname_map: dict[str, str] = {}


def _set_stdio_credential_fn(fn: Callable[[str], dict]) -> None:
    """Replace the credential resolver with a per-hostname disk-based lookup.

    Called once at server startup in stdio mode. fn is deriva.core.get_credential.
    """
    global _get_credential_fn
    _get_credential_fn = fn


def init_hostname_map(mapping: dict[str, str]) -> None:
    """Set the hostname remap table for outbound DERIVA connections.

    Called once at server startup from create_server(). Replaces the module-level
    map so that get_deriva_server() and get_hatrac_store() route tool hostnames
    through the internal network alias when running inside a Docker container.
    """
    global _hostname_map
    _hostname_map = dict(mapping)


def _remap(hostname: str) -> str:
    return _hostname_map.get(hostname, hostname)


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
        RuntimeError: If called outside a tool or resource handler context
                      and stdio credential fn is not set.
    """
    hostname = _remap(hostname)
    return DerivaServer("https", hostname, credentials=_get_credential_fn(hostname))


def get_hatrac_store(hostname: str):
    """Return an authenticated HatracStore for the current request context.

    HatracStore is the connection root for Hatrac object store operations.
    It is a separate binding from DerivaServer and operates on the /hatrac/ URL base.

    Args:
        hostname: Hostname of the DERIVA server (e.g. "deriva.example.org").

    Returns:
        A HatracStore authenticated with the current request credential.

    Raises:
        RuntimeError: If called outside a tool or resource handler context
                      and stdio credential fn is not set.
    """
    hostname = _remap(hostname)
    return HatracStore("https", hostname, credentials=_get_credential_fn(hostname))
