"""Credenza token introspection client (RFC 7662).

Validates an opaque bearer token by posting to Credenza's /introspect endpoint
using this server's client credentials. Returns an IntrospectionResult on success
or raises on inactive tokens or HTTP errors.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IntrospectionResult:
    """Parsed response from Credenza POST /introspect."""

    active: bool
    sub: str
    aud: list[str]
    exp: int | None
    # Full response payload retained for future use (e.g., claim forwarding)
    payload: dict


# TODO (Phase 2): implement async introspect(token: str) -> IntrospectionResult
