from __future__ import annotations

"""ASGI middleware for optional-auth (allow-anonymous) mode.

When DERIVA_MCP_ALLOW_ANONYMOUS=true, requests are allowed with or without a
bearer token:
  - With a valid token: normal Credenza validation and derived-token exchange,
    same as standard auth-required mode.  verify_token() sets the per-request
    credential and user-id contextvars as usual.
  - With no Authorization header: anonymous access.  Empty credential dict {}
    is set so all DERIVA calls proceed without credentials (public/anonymous
    catalog access).  mutation_allowed is set to False so anonymous users
    cannot execute mutating tools even when the kill-switch is off.
  - With an invalid/expired token: 401 response.  A client that sends a token
    must send a valid one -- a bad token is not silently downgraded to anonymous.
  - With a token present but no verifier configured (anonymous-only deployment):
    401 with an informative message.
"""

import logging
from typing import Any

from starlette.responses import PlainTextResponse

from ..context import set_current_credential, set_current_user_id, set_mutation_allowed
from ..telemetry import audit_event

logger = logging.getLogger(__name__)


class AnonymousPermitMiddleware:
    """ASGI middleware that permits unauthenticated requests in allow-anonymous mode.

    Intended for use with FastMCP when DERIVA_MCP_ALLOW_ANONYMOUS=true.  The
    FastMCP instance must be created WITHOUT token_verifier/auth (i.e. no
    RequireAuthMiddleware active); this middleware takes over all auth decisions.

    Starlette usage (after mcp.streamable_http_app() returns a Starlette app)::

        app = mcp.streamable_http_app()
        app.add_middleware(AnonymousPermitMiddleware, verifier=verifier_or_none)

    Equivalently, use build_http_app() from server.py which handles this
    automatically based on whether allow_anonymous is configured.
    """

    def __init__(self, app: Any, verifier: Any = None) -> None:
        """Args:
            app: The inner ASGI application.
            verifier: CredenzaTokenVerifier instance, or None for anonymous-only
                mode (no Credenza configured).  When None any provided bearer
                token is rejected.
        """
        self.app = app
        self._verifier = verifier

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            token: str | None = _extract_bearer_token(scope)

            if token is not None:
                if self._verifier is None:
                    logger.warning(
                        "allow_anonymous mode: bearer token provided but no verifier configured"
                    )
                    response = PlainTextResponse(
                        "Authentication is not configured on this server", status_code=401
                    )
                    await response(scope, receive, send)
                    return

                result = await self._verifier.verify_token(token)
                if result is None:
                    response = PlainTextResponse("Unauthorized", status_code=401)
                    await response(scope, receive, send)
                    return
                # verify_token() already set credential, user_id, and mutation_allowed
                # contextvars -- nothing more to do for the authenticated path.
            else:
                # No Authorization header -- anonymous request.
                set_current_credential({})
                set_current_user_id("anonymous")
                set_mutation_allowed(False)
                audit_event("anonymous_access")

        await self.app(scope, receive, send)


def _extract_bearer_token(scope: Any) -> str | None:
    """Return the Bearer token from the Authorization header, or None if absent."""
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            decoded = value.decode("latin-1")
            if decoded.lower().startswith("bearer "):
                return decoded[7:]
            break
    return None
