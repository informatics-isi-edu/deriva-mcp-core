from __future__ import annotations

"""Configuration model for deriva-mcp-core.

Settings are loaded from environment variables (DERIVA_MCP_ prefix) and an
optional env file. The env file is located by find_config_file(), which
searches the following paths in order and uses the first one found:

    /etc/deriva-mcp/deriva-mcp.env   (system-wide deployment)
    ~/deriva-mcp.env                 (user home directory)
    ./deriva-mcp.env                 (current working directory)

An explicit path can be supplied via the --config CLI argument or by calling
find_config_file(explicit="/path/to/file") directly. Environment variables
always take precedence over the env file.

Required variables (HTTP transport only -- not needed for stdio):
    DERIVA_MCP_CREDENZA_URL         -- Base URL of the Credenza instance
    DERIVA_MCP_SERVER_URL           -- Public HTTPS URL of this MCP server
    DERIVA_MCP_SERVER_RESOURCE      -- Resource identifier for this MCP server
    DERIVA_MCP_CLIENT_SECRET        -- Client secret for Credenza token exchange

Optional variables with defaults:
    DERIVA_MCP_DERIVA_RESOURCE      -- Resource identifier to exchange to
                                       (default: urn:deriva:rest:service:all)
    DERIVA_MCP_CLIENT_ID            -- Client ID for Credenza token exchange
                                       (default: deriva-mcp)

Optional debug flag:
    DERIVA_MCP_DEBUG                -- Set to true to enable DEBUG-level logging (default: false)

All other variables have sane defaults and are optional.
"""

from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_CONFIG_FILENAME = "deriva-mcp.env"

_DEFAULT_SEARCH_PATHS: list[Path] = [
    Path("/etc/deriva-mcp") / _CONFIG_FILENAME,
    Path.home() / _CONFIG_FILENAME,
    Path(".") / _CONFIG_FILENAME,
]


def find_config_file(explicit: str | None = None) -> str | None:
    """Return the path to the env file to load, or None if none is found.

    If explicit is given, that path is used directly and a FileNotFoundError
    is raised if it does not exist. Otherwise the default search paths are
    tried in order and the first existing file is returned.

    Args:
        explicit: Path supplied via --config; overrides search path.

    Returns:
        Absolute path string, or None if no config file was found.

    Raises:
        FileNotFoundError: If explicit is given but the file does not exist.
    """
    if explicit is not None:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"Config file not found: {explicit}")
        return str(p.resolve())

    for candidate in _DEFAULT_SEARCH_PATHS:
        if candidate.is_file():
            return str(candidate.resolve())

    return None


class Settings(BaseSettings):
    """Server configuration loaded from environment variables and optional env file."""

    model_config = SettingsConfigDict(
        env_prefix="DERIVA_MCP_",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Credenza endpoints and identity
    credenza_url: str = ""
    server_url: str = ""
    server_resource: str = ""
    deriva_resource: str = "urn:deriva:rest:service:all"

    # Hostname remapping for container-internal routing.
    # Maps external hostnames to internal network aliases, applied to all
    # outbound HTTP calls (tool hostnames, Credenza URL).
    # Useful when the public CONTAINER_HOSTNAME (e.g. "localhost") is
    # unreachable from inside the container but an internal alias (e.g.
    # "deriva") is available via the Docker internal network.
    # Set as JSON: DERIVA_MCP_HOSTNAME_MAP={"localhost":"deriva"}
    hostname_map: dict[str, str] = {}

    # Client credentials (confidential client for token exchange)
    client_id: str = "deriva-mcp"
    client_secret: str = ""

    # Token cache tuning
    token_cache_buffer_seconds: int = 60
    introspect_cache_ttl_seconds: int = 60

    # Audit logging
    audit_logfile_path: str = "deriva-mcp-audit.log"
    audit_use_syslog: bool = False

    # Access logging (uvicorn request log)
    access_logfile_path: str = "deriva-mcp-access.log"
    access_use_syslog: bool = False

    # Safety
    disable_mutating_tools: bool = True

    # Anonymous access. When True, unauthenticated requests are allowed alongside
    # authenticated ones (token is optional rather than required).
    # Anonymous requests receive empty DERIVA credentials (public/anonymous catalog
    # access) and read-only mutation permission (mutation_allowed=False).
    # When credenza_url is also set, provided tokens are still validated normally --
    # a bad token is rejected with 401 rather than silently downgraded to anonymous.
    # When credenza_url is NOT set (anonymous-only mode), any provided bearer token
    # is rejected with 401 and all traffic is anonymous.
    allow_anonymous: bool = False

    # Plugin allowlist. If None (unset), all discovered plugins are loaded.
    # If set (including empty list), only plugins whose entry point name appears
    # in the list are loaded. An empty list disables all external plugins.
    # Set as a comma-separated string: DERIVA_MCP_PLUGIN_ALLOWLIST=deriva-ml,my-plugin
    plugin_allowlist: list[str] | None = None

    @field_validator("plugin_allowlist", mode="before")
    @classmethod
    def _parse_plugin_allowlist(cls, v: object) -> object:
        """Accept a comma-separated string in addition to a JSON list."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    # Mutation claim requirement. If set, authenticated users must have a matching
    # claim in their token introspection payload to execute mutating tools (when
    # the killswitch is off). Specified as a JSON object where the key is the
    # claim name and the value is the required scalar or list of accepted values.
    # List values use OR semantics (any one match is sufficient).
    # Multiple keys use AND semantics (all must match).
    # Example: DERIVA_MCP_MUTATION_REQUIRED_CLAIM={"groups": ["deriva-mcp-mutator"]}
    # Example: DERIVA_MCP_MUTATION_REQUIRED_CLAIM={"mcp_can_mutate": true}
    # If unset, all authenticated users may mutate when the killswitch is off.
    mutation_required_claim: dict[str, Any] | None = None

    # Logging
    # Enable Python SysLogHandler for app logs (LOCAL1).  Leave False when
    # running under Docker with driver: syslog (compose handles forwarding).
    # Set True for non-Docker deployments where syslog is the only path to
    # a centralized log collector.
    app_use_syslog: bool = False
    debug: bool = False

    # TLS verification for outbound httpx calls (Credenza, ERMrest, Hatrac).
    # Accepts bool or a path string:
    #   true  -- verify with the system CA bundle (default)
    #   false -- disable TLS verification (dev/bypass only)
    #   /path/to/ca-bundle.pem -- verify using a custom CA certificate or bundle
    ssl_verify: bool | str = True

    @field_validator("ssl_verify", mode="before")
    @classmethod
    def _parse_ssl_verify(cls, v: object) -> object:
        """Coerce bool-like strings to bool; leave CA bundle paths as strings."""
        if isinstance(v, str):
            if v.lower() in ("true", "1", "yes"):
                return True
            if v.lower() in ("false", "0", "no"):
                return False
        return v

    def remap_url(self, url: str) -> str:
        """Rewrite url's hostname using hostname_map.

        Replaces the hostname component of url with the mapped value if a
        matching entry exists in hostname_map. Port is preserved. Useful for
        redirecting calls that use a public hostname (e.g. "localhost") to the
        corresponding internal network alias (e.g. "deriva") when running
        inside a Docker container where the public hostname resolves to the
        container itself.

        Returns url unchanged if no mapping applies.
        """
        if not self.hostname_map:
            return url
        parsed = urlparse(url)
        new_host = self.hostname_map.get(parsed.hostname or "", "")
        if not new_host:
            return url
        # Preserve port if present; otherwise netloc is just the hostname.
        if parsed.port:
            netloc = f"{new_host}:{parsed.port}"
        else:
            netloc = new_host
        return urlunparse(parsed._replace(netloc=netloc))

    def validate_for_http(self) -> None:
        """Raise ValueError if any field required for HTTP transport is empty.

        Call this during server startup when --transport http is selected.
        Not required for stdio transport (no Credenza interaction).

        When allow_anonymous=True and credenza_url is not set, no Credenza fields
        are required -- the server operates in anonymous-only mode where all
        traffic is treated as unauthenticated.  When credenza_url IS set (mixed
        mode: anonymous + authenticated), all Credenza fields are still validated
        so the authenticated path works correctly.
        """
        if self.allow_anonymous and not self.credenza_url:
            # Anonymous-only mode: no Credenza interaction needed.
            return
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


# Module-level singleton -- loaded once at import time using the search path.
# Tests override by constructing Settings directly with explicit values.
# CLI overrides by calling find_config_file(explicit=args.config) and passing
# the result to Settings(_env_file=path) before calling create_server().
settings = Settings(_env_file=find_config_file())
