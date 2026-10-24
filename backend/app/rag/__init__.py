"""Phase 5 ingestion, embeddings, hybrid retrieval, and evaluation."""

from app.rag.embeddings import EmbeddingProvider, HashEmbeddingProvider
from app.rag.ingest import IngestResult, ingest_knowledge, ingest_path, upsert_current_limits
from app.rag.retrieve import RetrievedChunk, hybrid_search

__all__ = [
    "EmbeddingProvider",
    "HashEmbeddingProvider",
    "IngestResult",
    "RetrievedChunk",
    "hybrid_search",
    "ingest_path",
    "ingest_knowledge",
    "upsert_current_limits",
]
