"""Per-request credential context for deriva-mcp-core.

The auth middleware (HTTP mode) or server startup (stdio mode) sets the current
credential via set_current_credential() before any tool or resource handler runs.

Tool and resource handlers access DERIVA via the public functions exported from
this module. Handlers never touch tokens directly.

Public API:
    get_catalog(hostname, catalog_id) -> ErmrestCatalog -- authenticated catalog connection
    get_hatrac_store(hostname)        -> HatracStore     -- connection root for Hatrac operations
    get_request_credential()          -> dict            -- credential dict for higher-level APIs
                                                            (e.g. DerivaML)
"""

import contextlib
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

# Per-request mutation permission. Set by the auth verifier in HTTP mode based on
# the mutation_required_claim config. Defaults to True so that stdio mode (where
# verify_token is never called) implicitly permits mutations for the local user.
_mutation_allowed: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mutation_allowed", default=True
)

# Per-request original bearer token (the MCP bearer token from the Authorization
# header). Set alongside _current_credential in HTTP mode. Used by TaskManager to
# re-exchange for a fresh derived token when a background task outlives one derived
# token window (30 minutes). None in stdio mode.
_current_bearer_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_bearer_token", default=None
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


def set_current_bearer_token(token: str) -> None:
    """Set the original MCP bearer token for the current request context.

    Called by the auth verifier (HTTP) so that TaskManager can re-exchange for
    a fresh derived token when a background task outlives one derived token window.
    Not intended for use in tool or resource handlers.
    """
    _current_bearer_token.set(token)


def get_request_bearer_token() -> str | None:
    """Return the original MCP bearer token for the current request context.

    Returns None in stdio mode. Used by ctx.submit_task() to capture the bearer
    token at submission time so background tasks can re-exchange credentials.
    """
    return _current_bearer_token.get()


def set_mutation_allowed(allowed: bool) -> None:
    """Set whether the current request principal is permitted to execute mutating tools.

    Called by the auth verifier (HTTP) after evaluating the mutation_required_claim
    config against the token introspection payload. Not intended for use in tool handlers.
    """
    _mutation_allowed.set(allowed)


def is_mutation_allowed() -> bool:
    """Return whether the current request principal may execute mutating tools.

    Returns True in stdio mode (default) and in HTTP mode when no claim check is
    configured or the required claim is present. Returns False in HTTP mode when a
    mutation_required_claim is configured but the principal's token does not satisfy it.
    """
    return _mutation_allowed.get()


# Module-level reference to the DerivedTokenCache singleton. Set by
# _set_token_cache() in HTTP mode; None in stdio mode.
_token_cache_ref: object | None = None


def _set_token_cache(cache: object) -> None:
    """Store a reference to the DerivedTokenCache for use by deriva_call().

    Called once at server startup in HTTP mode. Not for use in tool handlers.
    """
    global _token_cache_ref
    _token_cache_ref = cache


def _is_401(exc: Exception) -> bool:
    """Return True if exc is an HTTP 401 response from a downstream service.

    Uses duck typing so it works with both requests and httpx without an
    explicit import of either library. Also inspects the caused_by attribute
    because the deriva-py datapath API wraps HTTP errors in DataPathException
    with the original HTTPError stored as caused_by -- DataPathException itself
    does not expose a response attribute directly.
    """
    for candidate in (exc, getattr(exc, "caused_by", None)):
        if candidate is None:
            continue
        response = getattr(candidate, "response", None)
        if response is not None and getattr(response, "status_code", None) == 401:
            return True
    return False


def invalidate_current_derived_token() -> None:
    """Evict the derived token for the current principal from the cache.

    Call this after receiving a 401 from a downstream DERIVA service so that
    the next MCP request triggers a fresh token exchange rather than reusing
    the stale derived token. No-op in stdio mode or when no cache is set.
    """
    cache = _token_cache_ref
    if cache is None:
        return
    principal = _current_user_id.get()
    if principal is not None:
        cache.invalidate(principal)  # type: ignore[attr-defined]


@contextlib.contextmanager
def deriva_call():
    """Context manager for DERIVA tool calls that handles downstream 401s.

    Catches HTTP 401 responses from ERMrest or Hatrac, evicts the stale derived
    token from the cache, then re-raises so the caller's except block can return
    a normal error response. The next MCP request for this principal will get a
    fresh token exchange.

    Usage in a tool handler::

        try:
            with deriva_call():
                catalog = get_catalog(hostname, catalog_id)
                result = catalog.get(url).json()
                return json.dumps({...})
        except Exception as exc:
            return json.dumps({"error": str(exc)})
    """
    try:
        yield
    except Exception as exc:
        if _is_401(exc):
            invalidate_current_derived_token()
        raise


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
    map so that get_catalog() and get_hatrac_store() route tool hostnames
    through the internal network alias when running inside a Docker container.
    """
    global _hostname_map
    _hostname_map = dict(mapping)


def _remap(hostname: str) -> str:
    return _hostname_map.get(hostname, hostname)


# Optional callback fired whenever any tool calls get_catalog(). Registered
# by catalog.py at server startup so the RAG subsystem can auto-index schemas
# without requiring an explicit catalog introspection call first.
# Signature: (internal_hostname: str, catalog_id: str) -> None
_catalog_access_fn: Callable[[str, str], None] | None = None


def _set_catalog_access_fn(fn: Callable[[str, str], None]) -> None:
    """Register a callback fired on every get_catalog() call.

    Called once at server startup by catalog.py. The callback receives the
    remapped (internal) hostname and catalog_id. It must be non-blocking and
    exception-safe -- any errors are suppressed by the caller.
    """
    global _catalog_access_fn
    _catalog_access_fn = fn


def get_catalog(hostname: str, catalog_id: str):
    """Return an authenticated ErmrestCatalog for the current request context.

    Gets a ErmrestCatalog instance.
    Also fires the catalog-access callback (registered by catalog.py at startup)
    so that RAG schema indexing is triggered automatically on first access --
    without requiring an explicit catalog introspection tool call.

    Args:
        hostname: Hostname of the DERIVA server (e.g. "deriva.example.org").
        catalog_id: Catalog ID or alias.

    Returns:
        An ErmrestCatalog authenticated with the current request credential.
    """
    internal = _remap(hostname)
    catalog = DerivaServer("https", internal,
                           credentials=_get_credential_fn(internal)).connect_ermrest(catalog_id)
    if _catalog_access_fn is not None:
        try:
            _catalog_access_fn(internal, catalog_id)
        except Exception:
            pass
    return catalog


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
