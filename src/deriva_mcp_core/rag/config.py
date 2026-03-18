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

from __future__ import annotations

# TODO (Phase 4): implement RAGSettings
