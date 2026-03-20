from __future__ import annotations

"""RAG subsystem configuration.

Settings are read from environment variables with the DERIVA_MCP_RAG_ prefix
(nested under the top-level DERIVA_MCP_ namespace).

Vector backend selection:
    DERIVA_MCP_RAG_VECTOR_BACKEND=chroma (default)
        Uses embedded ChromaDB. Zero additional services.
        DERIVA_MCP_RAG_CHROMA_DIR sets the persistence directory.
        DERIVA_MCP_RAG_CHROMA_URL enables ChromaDB server mode instead.

    DERIVA_MCP_RAG_VECTOR_BACKEND=pgvector
        Uses PostgreSQL with the pgvector extension.
        DERIVA_MCP_RAG_PG_DSN is required.
        Recommended for multi-instance production deployments.
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RAGSettings(BaseSettings):
    """Configuration for the RAG subsystem."""

    model_config = SettingsConfigDict(
        env_prefix="DERIVA_MCP_RAG_",
        env_file=".env",
        extra="ignore",
    )

    enabled: bool = False
    vector_backend: str = "chroma"  # "chroma" or "pgvector"

    # Chroma-specific
    chroma_dir: str = "~/.deriva-mcp/chroma"
    chroma_url: str | None = None  # set to use Chroma HTTP server instead of embedded

    # pgvector-specific
    pg_dsn: str | None = None

    # General
    auto_update: bool = True  # crawl and update docs on server startup
    data_dir: str = "~/.deriva-mcp/rag"  # SHA cache and runtime sources

    @model_validator(mode="after")
    def _check_backend_config(self) -> "RAGSettings":
        if self.enabled and self.vector_backend == "pgvector" and not self.pg_dsn:
            raise ValueError(
                "DERIVA_MCP_RAG_PG_DSN is required when"
                " DERIVA_MCP_RAG_VECTOR_BACKEND=pgvector"
            )
        return self
