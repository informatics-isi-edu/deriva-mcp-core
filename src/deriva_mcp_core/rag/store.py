"""Vector store abstraction for the RAG subsystem.

VectorStore is a Protocol. Tool and RAG module code depends only on this
interface, never on a concrete backend. Two implementations are provided:

    ChromaVectorStore  -- embedded ChromaDB (default); zero additional services;
                          also supports ChromaDB server mode via chroma_url
    PgVectorStore      -- PostgreSQL with pgvector extension; recommended for
                          production multi-instance deployments

Backend is selected by RAGSettings.vector_backend and constructed once at
server startup. Plugin authors may supply their own VectorStore implementation
by passing it to PluginContext (see plugin authoring guide).
"""

from __future__ import annotations

# TODO (Phase 4): implement VectorStore protocol, Chunk, SearchResult,
#                 ChromaVectorStore, PgVectorStore
