from __future__ import annotations

"""FastMCP server factory and CLI entrypoint for deriva-mcp-core.

Transport modes:
    http  -- Production. Bearer tokens validated via Credenza introspection
             and exchange; per-request credential set in contextvar.
    stdio -- Local development only. Credential read from
             ~/.deriva/credential.json via deriva-py get_credential().

Usage:
    deriva-mcp-core                          # stdio (default)
    deriva-mcp-core --transport http         # HTTP on 127.0.0.1:8000
    deriva-mcp-core --transport http --host 0.0.0.0 --port 8000
"""

import argparse
import logging
import os

from deriva.core import get_credential as _get_credential
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import AuthSettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth.introspect_cache import IntrospectionCache
from .auth.token_cache import DerivedTokenCache
from .auth.verifier import CredenzaTokenVerifier
from .config import Settings
from .config import settings as _default_settings
from .context import _set_stdio_credential_fn
from .plugin.api import PluginContext, _set_plugin_context
from .plugin.loader import load_plugins
from .rag import register as _register_rag
from .telemetry import init_audit_logger
from .tools import catalog, entity, hatrac, query

logger = logging.getLogger(__name__)


def _init_logging(debug: bool = False) -> None:
    """Configure the root deriva_mcp_core logger.

    Uses the same format as Credenza: timestamp, process/thread, level, logger name.
    Attempts syslog first; falls back to a stream handler.
    """
    from logging.handlers import SysLogHandler

    fmt_stream = logging.Formatter(
        "%(asctime)s [%(process)d:%(threadName)s] [%(levelname)s] [%(name)s] - %(message)s"
    )
    fmt_syslog = logging.Formatter(
        "[%(process)d:%(threadName)s] [%(levelname)s] [%(name)s] - %(message)s"
    )

    log_handler: logging.Handler = logging.StreamHandler()
    log_handler.setFormatter(fmt_stream)

    syslog_socket = "/dev/log"
    if os.path.exists(syslog_socket) and os.access(syslog_socket, os.W_OK):
        try:
            sh = SysLogHandler(address=syslog_socket, facility=SysLogHandler.LOG_LOCAL1)
            sh.ident = "deriva-mcp-core: "
            sh.setFormatter(fmt_syslog)
            log_handler = sh
        except Exception:
            pass  # keep the stream handler

    root = logging.getLogger("deriva_mcp_core")
    root.addHandler(log_handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.propagate = False


def create_server(
    transport: str = "stdio",
    settings: Settings | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
):
    """Create and configure the FastMCP server.

    Args:
        transport: 'stdio' or 'http'.
        settings: Configuration instance. Defaults to the module-level singleton.
        host: Bind address for http transport.
        port: Bind port for http transport.

    Returns:
        A configured FastMCP instance ready to run.
    """
    cfg = settings or _default_settings

    init_audit_logger(filename=cfg.audit_logfile_path, use_syslog=cfg.audit_use_syslog)

    if cfg.disable_mutating_tools:
        logger.warning(
            "Mutating tools are DISABLED (DERIVA_MCP_DISABLE_MUTATING_TOOLS=true). "
            "All tools registered with mutates=True will return an error without executing. "
            "Set DERIVA_MCP_DISABLE_MUTATING_TOOLS=false to enable catalog writes."
        )
    else:
        logger.info(
            "Mutating tools are ENABLED (DERIVA_MCP_DISABLE_MUTATING_TOOLS=false)."
        )

    if transport == "http":
        cfg.validate_for_http()
        token_cache = DerivedTokenCache(cfg)
        introspect_cache = IntrospectionCache(cfg)
        verifier = CredenzaTokenVerifier(cfg, token_cache, introspect_cache)
        auth = AuthSettings(
            issuer_url=cfg.credenza_url,
            resource_server_url=cfg.server_url,
        )
        mcp = FastMCP("deriva-mcp-core",
                      token_verifier=verifier,
                      auth=auth,
                      host=host,
                      port=port,
                      streamable_http_path="/")
    else:
        # stdio: read per-hostname credentials from local disk at call time
        _set_stdio_credential_fn(_get_credential)
        mcp = FastMCP("deriva-mcp-core")

    # Health endpoint -- no auth, suitable for Docker health probes
    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    # Build the plugin context and register built-in tool modules
    ctx = PluginContext(mcp, disable_mutating_tools=cfg.disable_mutating_tools)
    _set_plugin_context(ctx)

    for module in [catalog, entity, query, hatrac]:
        module.register(ctx)

    _register_rag(ctx)

    # Discover and register external plugins (entry points)
    load_plugins(ctx)

    return mcp


def main() -> None:
    """CLI entrypoint for the deriva-mcp-core server."""
    parser = argparse.ArgumentParser(
        prog="deriva-mcp-core",
        description="deriva-mcp-core MCP server",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for http transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for http transport (default: 8000)",
    )
    args = parser.parse_args()

    mcp = create_server(transport=args.transport, host=args.host, port=args.port)
    _init_logging()
    fastmcp_transport = "streamable-http" if args.transport == "http" else args.transport
    mcp.run(transport=fastmcp_transport)
