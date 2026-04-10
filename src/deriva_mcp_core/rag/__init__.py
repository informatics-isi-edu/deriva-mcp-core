from __future__ import annotations

"""Built-in RAG subsystem for deriva-mcp-core.

Indexes DERIVA documentation (deriva-py, ermrest, chaise) and catalog schemas
into a vector store, then exposes semantic search as MCP tools.

MCP tools registered by register(ctx):
    rag_search          -- semantic search across docs and schema indexes
    rag_update_docs     -- incremental documentation update (SHA delta)
    rag_index_schema    -- manual schema reindex trigger
    rag_status          -- per-source chunk counts and timestamps

Submodules:
    config   -- RAGSettings (DERIVA_MCP_RAG_* env vars)
    store    -- VectorStore protocol + ChromaVectorStore + PgVectorStore
    chunker  -- Markdown-aware document chunking
    crawler  -- GitHub repo crawler (Trees API, SHA change detection)
    docs     -- Documentation source ingestion pipeline
    schema   -- Catalog schema indexing (visibility class isolation)
    tools    -- MCP tool registration (register, get_rag_store)
"""

from .tools import get_rag_status, get_rag_store, register

__all__ = ["get_rag_status", "get_rag_store", "register"]
