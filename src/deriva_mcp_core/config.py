from __future__ import annotations

"""Configuration model for deriva-mcp-core.

All settings are read from environment variables with the DERIVA_MCP_ prefix.
An optional .env file is also supported (lowest priority, overridden by env vars).

Required variables (server will not start without these in HTTP mode):
    DERIVA_MCP_CREDENZA_URL         -- Base URL of the Credenza instance
    DERIVA_MCP_SERVER_RESOURCE      -- Resource identifier for this MCP server
    DERIVA_MCP_DERIVA_RESOURCE      -- Resource identifier to exchange to (DERIVA REST)
    DERIVA_MCP_CLIENT_ID            -- Client ID for Credenza token exchange
    DERIVA_MCP_CLIENT_SECRET        -- Client secret for Credenza token exchange

Optional variables:
    DERIVA_MCP_TOKEN_CACHE_BUFFER_SECONDS  -- Near-expiry buffer for derived token cache
                                             (default: 60)
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration loaded from environment variables and optional .env file."""

    model_config = SettingsConfigDict(
        env_prefix="DERIVA_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Credenza endpoints and identity
    credenza_url: str = ""
    server_url: str = ""
    server_resource: str = ""
    deriva_resource: str = ""

    # Client credentials (confidential client for token exchange)
    client_id: str = ""
    client_secret: str = ""

    # Token cache tuning
    token_cache_buffer_seconds: int = 60
    introspect_cache_ttl_seconds: int = 60

    # Audit logging
    audit_logfile_path: str = "deriva-mcp-audit.log"
    audit_use_syslog: bool = False

    # Safety
    disable_mutating_tools: bool = True

    def validate_for_http(self) -> None:
        """Raise ValueError if any field required for HTTP transport is empty.

        Call this during server startup when --transport streamable-http is selected.
        Not required for stdio transport (no Credenza interaction).
        """
        required = {
            "DERIVA_MCP_CREDENZA_URL": self.credenza_url,
            "DERIVA_MCP_SERVER_URL": self.server_url,
            "DERIVA_MCP_SERVER_RESOURCE": self.server_resource,
            "DERIVA_MCP_DERIVA_RESOURCE": self.deriva_resource,
            "DERIVA_MCP_CLIENT_ID": self.client_id,
            "DERIVA_MCP_CLIENT_SECRET": self.client_secret,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "Missing required environment variables for HTTP transport: " + ", ".join(missing)
            )


# Module-level singleton -- loaded once at import time.
# Tests can override by constructing Settings directly with explicit values.
settings = Settings()
