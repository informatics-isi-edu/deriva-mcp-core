from __future__ import annotations

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
import logging
from mcp.server.auth.provider import AccessToken
from .exchange import ExchangeError
from .introspect import IntrospectionError, TokenInactiveError
from .introspect_cache import IntrospectionCache
from .token_cache import DerivedTokenCache
from ..config import Settings
from ..context import set_current_credential, set_current_user_id
from ..telemetry import audit_event

logger = logging.getLogger(__name__)


class CredenzaTokenVerifier:
    """TokenVerifier implementation that validates tokens via Credenza introspection.

    Passed to FastMCP(token_verifier=CredenzaTokenVerifier(...)) at server startup.
    FastMCP calls verify_token() on every authenticated request; returning None
    causes FastMCP to respond with 401 automatically.
    """

    def __init__(
        self,
        settings: Settings,
        token_cache: DerivedTokenCache,
        introspect_cache: IntrospectionCache,
    ) -> None:
        self._settings = settings
        self._token_cache = token_cache
        self._introspect_cache = introspect_cache

    async def verify_token(self, token: str) -> AccessToken | None:
        """Validate a bearer token and set the per-request DERIVA credential.

        Returns an AccessToken on success, or None to trigger a 401 response.
        All failures (inactive token, audience mismatch, exchange failure) return None.
        """
        # Step 1: introspect the token via Credenza (cached)
        try:
            result = await self._introspect_cache.get(token)
        except TokenInactiveError:
            audit_event("token_inactive")
            return None
        except (IntrospectionError, Exception):
            logger.warning("Token introspection failed", exc_info=True)
            audit_event("token_introspection_failed")
            return None

        # Principal is always derived from the MCP-token introspection result
        principal = f"{result.iss}/{result.sub}" if result.iss else result.sub

        # Step 2: validate the audience
        if self._settings.server_resource not in result.aud:
            logger.debug(
                "Token audience mismatch: expected %s in %s",
                self._settings.server_resource,
                result.aud,
            )
            audit_event("token_audience_mismatch", principal=principal)
            return None

        # Step 3: obtain a DERIVA-scoped derived token via the cache
        try:
            derived_token = await self._token_cache.get(principal, token)
        except (ExchangeError, Exception):
            logger.warning("Token exchange failed for principal=%s", principal, exc_info=True)
            audit_event("token_exchange_failed", principal=principal)
            return None

        # Step 4: set the per-request credential and user identity.
        set_current_credential({"bearer-token": derived_token})
        set_current_user_id(principal)

        display = (
            result.payload.get("email") or result.payload.get("preferred_username") or result.sub
        )
        logger.info(
            "Authenticated: principal=%s display=%s aud=%s",
            principal,
            display,
            result.aud,
        )
        audit_event("token_verified", display=display)

        return AccessToken(
            token=derived_token,
            client_id=principal,
            scopes=[],
            expires_at=result.exp,
            resource=self._settings.server_resource,
        )
