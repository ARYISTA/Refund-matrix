"""Hybrid lexical/vector retrieval with an explicit similarity threshold."""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.rag.embeddings import EmbeddingProvider


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    source_path: str
    content: str
    score: float


def hybrid_search(
    session: Session,
    query: str,
    embedder: EmbeddingProvider,
    *,
    top_k: int = 5,
    min_similarity: float = 0.35,
) -> list[RetrievedChunk]:
    if not query.strip():
        raise ValueError("query must not be empty")
    if top_k < 1:
        raise ValueError("top_k must be positive")
    vector = embedder.embed(query)
    rows = session.execute(
        text(
            """
            SELECT c.id, d.source_path, c.content,
                   1 - (c.embedding <=> CAST(:embedding AS vector)) AS vector_score,
                   ts_rank(
                       to_tsvector('simple', c.content),
                       plainto_tsquery('simple', :query)
                   ) AS lexical_score
            FROM knowledge_chunks c
            JOIN knowledge_documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL
              AND (c.embedding <=> CAST(:embedding AS vector)) <= :distance
            ORDER BY (0.7 * (1 - (c.embedding <=> CAST(:embedding AS vector)))
                      + 0.3 * ts_rank(
                          to_tsvector('simple', c.content),
                          plainto_tsquery('simple', :query)
                      )) DESC
            LIMIT :top_k
            """
        ),
        {
            "embedding": "[" + ",".join(str(value) for value in vector) + "]",
            "query": query.strip(),
            "distance": 1.0 - min_similarity,
            "top_k": top_k,
        },
    )
    return [
        RetrievedChunk(
            chunk_id=str(row.id),
            source_path=str(row.source_path),
            content=str(row.content),
            score=float(0.7 * row.vector_score + 0.3 * row.lexical_score),
        )
        for row in rows
    ]
