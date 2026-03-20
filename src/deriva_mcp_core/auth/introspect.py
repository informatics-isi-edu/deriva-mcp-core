from __future__ import annotations

"""Credenza token introspection client (RFC 7662).

Validates an opaque bearer token by posting to Credenza's /introspect endpoint
using this server's client credentials. Returns an IntrospectionResult on success
or raises on inactive tokens or HTTP errors.
"""
import httpx
from dataclasses import dataclass
from ..config import Settings


class TokenInactiveError(Exception):
    """Raised when the introspected token is inactive or expired."""


class IntrospectionError(Exception):
    """Raised on unexpected introspection responses (missing fields, HTTP errors)."""


@dataclass
class IntrospectionResult:
    """Parsed response from Credenza POST /introspect."""

    active: bool
    sub: str
    aud: list[str]
    exp: int | None
    iss: str | None
    # Full response payload retained for future use (e.g., claim forwarding)
    payload: dict


async def introspect(token: str, settings: Settings) -> IntrospectionResult:
    """Introspect a bearer token against Credenza.

    Args:
        token: The opaque bearer token to validate.
        settings: Server configuration supplying the Credenza URL and client credentials.

    Returns:
        IntrospectionResult with active=True and parsed claims.

    Raises:
        TokenInactiveError: Token is inactive or expired.
        IntrospectionError: Introspection response is missing required fields.
        httpx.HTTPStatusError: Credenza returned a non-2xx response.
    """
    url = f"{settings.credenza_url.rstrip('/')}/introspect"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            data={"token": token, "resource": settings.server_resource},
            auth=(settings.client_id, settings.client_secret),
        )
        response.raise_for_status()
        payload = response.json()

    if not payload.get("active"):
        raise TokenInactiveError("Token is inactive or expired")

    sub = payload.get("sub")
    if not sub:
        raise IntrospectionError("Introspection response missing required field: sub")

    # RFC 7662: aud may be a JSON string or array
    aud = payload.get("aud", [])
    if isinstance(aud, str):
        aud = [aud]

    return IntrospectionResult(
        active=True,
        sub=sub,
        aud=aud,
        exp=payload.get("exp"),
        iss=payload.get("iss"),
        payload=payload,
    )
