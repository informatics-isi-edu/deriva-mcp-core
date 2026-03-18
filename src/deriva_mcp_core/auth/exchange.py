"""Credenza token exchange client (RFC 8693).

Exchanges a subject token (MCP-scoped) for a derived token bound to the configured
DERIVA resource. Uses this server's confidential client credentials to authenticate
the exchange request.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExchangeResult:
    """Result of a successful token exchange."""

    access_token: str
    expires_in: int
    # Absolute expiry as a Unix timestamp, computed at exchange time.
    expires_at: float


# TODO (Phase 2): implement async exchange(subject_token: str, target_resource: str) -> ExchangeResult
