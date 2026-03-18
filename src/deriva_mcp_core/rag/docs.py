"""Documentation source ingestion pipeline.

Manages the set of GitHub-hosted Markdown documentation sources and their
ingestion into the vector store.

Default sources (indexed as core DERIVA documentation):
    deriva-py-docs  -- informatics-isi-edu/deriva-py, branch master, path docs/
    ermrest-docs    -- informatics-isi-edu/ermrest, branch master, path docs/
    chaise-docs     -- informatics-isi-edu/chaise, branch master, path docs/

Additional sources can be registered at runtime via the rag_add_source tool
(out of scope for Phase 4 -- reserved for future extension).

Key functions:
    ingest_docs(source_name?, force?)  -- full crawl and ingestion
    update_docs(source_name?)          -- incremental update (SHA delta)
"""

from __future__ import annotations

# TODO (Phase 4): implement ingest_docs(), update_docs(), SourceConfig
