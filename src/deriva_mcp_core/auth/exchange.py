from __future__ import annotations

"""Credenza token exchange client (RFC 8693).

Exchanges a subject token (MCP-scoped) for a derived token bound to the configured
DERIVA resource. Uses this server's confidential client credentials to authenticate
the exchange request.
"""


import time
import httpx3
from dataclasses import dataclass
from ..config import Settings


class ExchangeError(Exception):
    """Raised on unexpected token exchange responses (missing fields, HTTP errors)."""


@dataclass
class ExchangeResult:
    """Result of a successful token exchange."""

    access_token: str
    expires_in: int
    # Absolute expiry as a Unix timestamp, computed at exchange time.
    expires_at: float


async def exchange(subject_token: str, settings: Settings) -> ExchangeResult:
    """Exchange a subject token for a DERIVA-scoped derived token.

    Args:
        subject_token: The MCP-scoped bearer token to exchange.
        settings: Server configuration supplying the Credenza URL, client credentials,
            and target DERIVA resource identifier.

    Returns:
        ExchangeResult containing the derived token and its expiry.

    Raises:
        ExchangeError: Exchange response is missing required fields.
        httpx.HTTPStatusError: Credenza returned a non-2xx response.
    """
    url = f"{settings.credenza_url.rstrip('/')}/token"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": subject_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "resource": settings.deriva_resource,
            },
            auth=(settings.client_id, settings.client_secret),
        )
        response.raise_for_status()
        payload = response.json()

    access_token = payload.get("access_token")
    if not access_token:
        raise ExchangeError("Token exchange response missing required field: access_token")

    expires_in = int(payload.get("expires_in", 1800))

    return ExchangeResult(
        access_token=access_token,
        expires_in=expires_in,
        expires_at=time.time() + expires_in,
    )
