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
import asyncio
import logging
import os
import time
from urllib.parse import urlparse

from deriva.core import get_credential as _get_credential
from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import AuthSettings
from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth.introspect_cache import IntrospectionCache
from .auth.token_cache import DerivedTokenCache
from .auth.verifier import CredenzaTokenVerifier
from .config import Settings, find_config_file
from .config import settings as _default_settings
from .context import _set_stdio_credential_fn, _set_token_cache, init_hostname_map
from .plugin.api import PluginContext, _set_plugin_context
from .plugin.loader import load_plugins
from .rag import register as _register_rag
from .tasks.manager import TaskManager, _set_task_manager
from .telemetry import init_audit_logger
from .tools import annotation, catalog, entity, hatrac, prompts, query, resources, schema, tasks, vocabulary

logger = logging.getLogger(__name__)


def _merge_env(env_file: str | None) -> dict[str, str]:
    """Return a merged env dict: env file values overlaid by os.environ.

    Reads a simple KEY=VALUE env file (comments and blank lines ignored,
    inline quotes stripped). os.environ always wins over file values so that
    runtime overrides are respected without modifying the file.
    """
    merged: dict[str, str] = {}
    if env_file:
        try:
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    merged[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            logger.warning("Could not read env file: %s", env_file)
    merged.update(os.environ)
    return merged


def _init_logging(debug: bool = False, app_use_syslog: bool = False) -> None:  # pragma: no cover
    """Configure logging for deriva-mcp-core and its plugins.

    Strategy: root stays at WARNING so third-party library noise (mcp internals,
    chromadb, httpx, uvicorn request details, etc.) is suppressed regardless of
    the debug flag. Only deriva_mcp_core and loaded plugin loggers are promoted
    to DEBUG or INFO. Plugin loggers are set after load via set_plugin_log_level().

    Always adds a stderr StreamHandler (for docker logs and local dev).
    Optionally adds a SysLogHandler on LOCAL1 when app_use_syslog is True,
    for non-Docker deployments where syslog is the only path to a centralized
    collector.  In Docker, driver: syslog in compose already forwards stderr,
    so enabling this would duplicate every app log line.

    Audit and access logs have their own SysLogHandlers (LOCAL1/LOCAL2)
    controlled by separate config flags.
    """
    fmt_stream = logging.Formatter(
        "%(asctime)s [%(process)d:%(threadName)s] [%(levelname)s] [%(name)s] - %(message)s"
    )

    app_level = logging.DEBUG if debug else logging.INFO

    # Root handler receives output from all loggers that propagate.
    # Root level stays at WARNING so third-party libs are quiet by default.
    root = logging.getLogger()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt_stream)
    root.addHandler(stream_handler)
    root.setLevel(logging.WARNING)

    if app_use_syslog:
        syslog_socket = "/dev/log"
        if os.path.exists(syslog_socket) and os.access(syslog_socket, os.W_OK):
            from logging.handlers import SysLogHandler

            try:
                sh = SysLogHandler(address=syslog_socket, facility=SysLogHandler.LOG_LOCAL1)
                sh.ident = "deriva-mcp-core: "
                sh.setFormatter(logging.Formatter(
                    "[%(process)d:%(threadName)s] [%(levelname)s] [%(name)s] - %(message)s"
                ))
                root.addHandler(sh)
            except Exception:
                pass

    # Application logger -- propagates to root handler.
    logging.getLogger("deriva_mcp_core").setLevel(app_level)

    # Give mcp and uvicorn their own handler at INFO and disable propagation so
    # they don't double-print through root and stay quiet in debug mode.
    for lib_name in ("mcp", "uvicorn"):
        lib_log = logging.getLogger(lib_name)
        lib_log.handlers = []
        lib_log.addHandler(stream_handler)
        lib_log.setLevel(logging.INFO)
        lib_log.propagate = False

    # Suppress per-request noise from MCP internals -- these emit an INFO line
    # for every tool call and session termination which adds no diagnostic value.
    for noisy in ("mcp.server.lowlevel.server", "mcp.server.streamable_http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Detach uvicorn.access from the main log stream.  Handlers are added
    # later by _init_access_logging() once settings are available.
    access_log = logging.getLogger("uvicorn.access")
    access_log.handlers = []
    access_log.propagate = False
    access_log.setLevel(logging.INFO)


def _set_plugin_log_level(plugin_packages: list[str], debug: bool = False) -> None:  # pragma: no cover
    """Set log level for each loaded plugin's top-level package logger.

    Called after load_plugins() so that plugin loggers (e.g. facebase_deriva_mcp_plugin)
    are promoted to the same DEBUG/INFO level as deriva_mcp_core rather than
    inheriting WARNING from root.
    """
    level = logging.DEBUG if debug else logging.INFO
    for pkg in plugin_packages:
        logging.getLogger(pkg).setLevel(level)


class _HealthCheckThrottle(logging.Filter):  # pragma: no cover
    _interval = 600.0
    _last_logged: float = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        if "/health" not in record.getMessage():
            return True
        now = time.monotonic()
        if now - self.__class__._last_logged >= self.__class__._interval:
            self.__class__._last_logged = now
            return True
        return False


def _init_access_logging(cfg: Settings) -> None:  # pragma: no cover
    """Route uvicorn access logs to a dedicated file and/or syslog facility.

    Keeps request-level auditing available without cluttering the main
    application log.  Uses LOG_LOCAL2 for syslog so rsyslog can route
    access logs to a separate file from the application log (LOG_LOCAL1).
    """
    from logging.handlers import RotatingFileHandler, SysLogHandler

    fmt = logging.Formatter("%(asctime)s %(message)s")
    access_log = logging.getLogger("uvicorn.access")
    access_log.addFilter(_HealthCheckThrottle())

    if cfg.access_logfile_path:
        fh = RotatingFileHandler(cfg.access_logfile_path, maxBytes=50_000_000, backupCount=5)
        fh.setFormatter(fmt)
        access_log.addHandler(fh)

    if cfg.access_use_syslog:
        syslog_socket = "/dev/log"
        if os.path.exists(syslog_socket) and os.access(syslog_socket, os.W_OK):
            try:
                sh = SysLogHandler(address=syslog_socket, facility=SysLogHandler.LOG_LOCAL2)
                sh.ident = "deriva-mcp-access: "
                sh.setFormatter(logging.Formatter("%(message)s"))
                access_log.addHandler(sh)
            except Exception:
                pass


def create_server(
    transport: str = "stdio",
    settings: Settings | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    env_file: str | None = None,
):
    """Create and configure the FastMCP server.

    Args:
        transport: 'stdio' or 'http'.
        settings: Configuration instance. Defaults to the module-level singleton.
        host: Bind address for http transport.
        port: Bind port for http transport.
        env_file: Path to the env file resolved at startup. Forwarded to RAG
            settings so RAG variables in deriva-mcp.env are picked up.

    Returns:
        A configured FastMCP instance ready to run.
    """
    cfg = settings or _default_settings

    if cfg.hostname_map:
        logger.info("Hostname map active: %s", cfg.hostname_map)
        init_hostname_map(cfg.hostname_map)

    init_audit_logger(use_syslog=cfg.audit_use_syslog)

    if cfg.disable_mutating_tools:
        logger.warning(
            "Mutating tools are DISABLED (DERIVA_MCP_DISABLE_MUTATING_TOOLS=true). "
            "All tools registered with mutates=True will return an error without executing. "
            "Set DERIVA_MCP_DISABLE_MUTATING_TOOLS=false to enable catalog writes."
        )
    else:
        logger.info("Mutating tools are ENABLED (DERIVA_MCP_DISABLE_MUTATING_TOOLS=false).")

    # Server instructions sent to clients at init time via MCP protocol.
    _instructions = (
        "DERIVA MCP server for catalog introspection, querying, entity CRUD, "
        "annotation management, and file operations. Call the relevant guide "
        "prompt (query_guide, entity_guide, annotation_guide, catalog_guide) "
        "before your first use of each tool group in a conversation."
    )

    if transport == "http":
        cfg.validate_for_http()

        # Build Credenza components only when a Credenza URL is configured.
        # In anonymous-only mode (allow_anonymous=True, no credenza_url) these
        # are omitted entirely -- no introspection or token exchange is needed.
        token_cache = None
        verifier = None
        if cfg.credenza_url:
            token_cache = DerivedTokenCache(cfg)
            _set_token_cache(token_cache)
            introspect_cache = IntrospectionCache(cfg)
            verifier = CredenzaTokenVerifier(cfg, token_cache, introspect_cache)

        if cfg.allow_anonymous:
            # Anonymous mode: FastMCP has no built-in auth enforcement.
            # AnonymousPermitMiddleware (added by build_http_app) handles
            # token extraction, validation, and anonymous fallback.
            # FastMCP must NOT receive token_verifier/auth here -- that would
            # activate RequireAuthMiddleware which conflicts with anonymous access.
            mcp = FastMCP(
                "deriva-mcp-core",
                instructions=_instructions,
                host=host,
                port=port,
                streamable_http_path="/",
                stateless_http=True,
            )
            # Stash the verifier (may be None for anonymous-only mode) so
            # build_http_app() can wire up AnonymousPermitMiddleware correctly.
            mcp._allow_anonymous_verifier = verifier  # type: ignore[attr-defined]

            # Register the RFC 9728 protected resource metadata endpoint manually.
            # FastMCP only registers this when auth= is passed, but auth= requires
            # token_verifier which conflicts with anonymous mode. Without this
            # endpoint, OAuth clients cannot discover the authorization server and
            # fall back to constructing a stripped URL (e.g. https://host/authorize
            # instead of https://host/authn/authorize).
            if cfg.credenza_url and cfg.server_url:
                _resource_url = AnyHttpUrl(cfg.server_url)
                _prm = ProtectedResourceMetadata(
                    resource=_resource_url,
                    authorization_servers=[AnyHttpUrl(cfg.credenza_url)],
                )
                _prm_path = urlparse(str(build_resource_metadata_url(_resource_url))).path

                @mcp.custom_route(_prm_path, methods=["GET"])
                async def _oauth_protected_resource(request: Request) -> Response:
                    return PydanticJSONResponse(
                        content=_prm,
                        headers={"Cache-Control": "public, max-age=3600"},
                    )
        else:
            auth = AuthSettings(
                issuer_url=cfg.credenza_url,
                resource_server_url=cfg.server_url,
            )
            mcp = FastMCP(
                "deriva-mcp-core",
                instructions=_instructions,
                token_verifier=verifier,
                auth=auth,
                host=host,
                port=port,
                streamable_http_path="/",
                stateless_http=True,
            )

        task_manager = TaskManager(token_cache=token_cache)
    else:
        # stdio: read per-hostname credentials from local disk at call time
        _set_stdio_credential_fn(_get_credential)
        mcp = FastMCP("deriva-mcp-core", instructions=_instructions)
        task_manager = TaskManager(token_cache=None)

    _set_task_manager(task_manager)

    # Health endpoint -- no auth, suitable for Docker health probes
    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    # Build the plugin context and register built-in tool modules
    ctx = PluginContext(
        mcp,
        disable_mutating_tools=cfg.disable_mutating_tools,
        mutation_required_claim=cfg.mutation_required_claim,
        task_manager=task_manager,
        env=_merge_env(env_file),
    )
    _set_plugin_context(ctx)

    for module in [catalog, entity, query, hatrac, vocabulary, annotation, schema, tasks, prompts, resources]:
        module.register(ctx)

    # Load plugins before RAG so plugin-declared web/data sources are visible
    # to rag/tools.py when it builds all_sources from ctx._rag_web_sources et al.
    loaded_pkgs = load_plugins(ctx, allowlist=cfg.plugin_allowlist)
    _set_plugin_log_level(loaded_pkgs, debug=cfg.debug)

    _register_rag(ctx, env_file=env_file)

    return mcp


_MISSING = object()


def build_http_app(mcp):
    """Return the Starlette ASGI app for HTTP transport.

    In normal auth-required mode this is equivalent to mcp.streamable_http_app().

    In allow-anonymous mode (detected via the _allow_anonymous_verifier attribute
    set by create_server()) this wraps the app with AnonymousPermitMiddleware so
    that unauthenticated requests receive empty DERIVA credentials instead of a 401.

    Use this instead of mcp.streamable_http_app() both in production (main()) and
    in tests so the anonymous middleware is active when needed.
    """
    app = mcp.streamable_http_app()
    verifier = getattr(mcp, "_allow_anonymous_verifier", _MISSING)
    if verifier is not _MISSING:
        from .auth.anonymous import AnonymousPermitMiddleware
        app.add_middleware(AnonymousPermitMiddleware, verifier=verifier)
    return app


def main() -> None:  # pragma: no cover
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
    parser.add_argument(
        "--config",
        default=None,
        metavar="FILE",
        help=(
            "Path to env file (default: search /etc/deriva-mcp/deriva-mcp.env,"
            " ~/deriva-mcp.env, ./deriva-mcp.env)"
        ),
    )
    args = parser.parse_args()

    try:
        config_path = find_config_file(explicit=args.config)
    except FileNotFoundError as exc:
        parser.error(str(exc))

    cfg = Settings(_env_file=config_path)
    _init_logging(debug=cfg.debug, app_use_syslog=cfg.app_use_syslog)
    _init_access_logging(cfg)
    if config_path:
        logger.info("Loaded configuration from: %s", config_path)
    else:
        logger.info("No config file found; using environment variables and defaults")

    async def _run() -> None:
        mcp = create_server(
            transport=args.transport,
            settings=cfg,
            host=args.host,
            port=args.port,
            env_file=config_path,
        )
        if args.transport == "http":
            import uvicorn
            starlette_app = build_http_app(mcp)
            uv_config = uvicorn.Config(
                starlette_app,
                host=args.host,
                port=args.port,
                log_level="debug" if cfg.debug else "info",
                log_config=None,
            )
            await uvicorn.Server(uv_config).serve()
        else:
            await mcp.run_stdio_async()

    asyncio.run(_run())
