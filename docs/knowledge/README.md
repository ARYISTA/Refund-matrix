# Knowledge documents

Place Markdown, plain-text, or PDF policy/support documents in this directory.

Phase 5 ingestion also indexes `backend/app/policy/policy.yaml` as the authoritative policy
source. Documents are chunked, embedded, and stored in PostgreSQL with pgvector. Retrieval
returns no result when the vector similarity is below `RAG_MIN_SIMILARITY`.
