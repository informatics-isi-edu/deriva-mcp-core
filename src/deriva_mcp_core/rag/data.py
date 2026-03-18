"""Generic per-user data indexing primitives for the RAG subsystem.

Provides a generic pipeline for indexing catalog table rows into the vector
store, keyed by user identity. Plugins (e.g., deriva-ml) extend the serialization
by implementing the RowSerializer protocol and calling index_table_data() directly.

Source naming: data:{hostname}:{catalog_id}:{user_id}

DERIVA system columns excluded from generic serialization:
    RID, RCT, RMT, RCB, RMB

Public API for plugin authors (import from deriva_mcp_core.rag.data):
    RowSerializer       -- Protocol for custom row-to-Markdown serialization
    index_table_data()  -- Core indexing primitive; callable from lifecycle hooks
    data_source_name()  -- Source name helper (consistent naming across plugins)
"""

from __future__ import annotations

# TODO (Phase 4): implement RowSerializer protocol, index_table_data(),
#                 data_source_name(), staleness detection, generic row serializer
