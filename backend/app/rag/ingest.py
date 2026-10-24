"""Load local knowledge files, chunk them, embed them, and persist idempotently."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import yaml
from pypdf import PdfReader
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import KnowledgeChunk, KnowledgeDocument
from app.policy.config import PolicyConfig
from app.rag.embeddings import EmbeddingProvider

SUPPORTED = {".md": "markdown", ".txt": "text", ".pdf": "pdf", ".yaml": "yaml", ".yml": "yaml"}


@dataclass(frozen=True)
class IngestResult:
    source_path: str
    chunks: int
    changed: bool


def upsert_current_limits(
    session: Session, policy: PolicyConfig, embedder: EmbeddingProvider
) -> IngestResult:
    """Keep the generated limits answer synchronized with the live policy setting."""
    content = (
        f"Current refund limits: refunds must be requested within {policy.return_window_days} days. "
        f"Eligible refunds below Rs {policy.auto_approve_below_paise // 100:,} are automatically "
        f"approved when every hard rule passes. Refunds at or above that amount go to manager "
        f"review. More than {policy.max_refunds_per_customer_30d} refunds in 30 days also triggers review."
    )
    source_path = "generated:current-limits"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    existing = session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.source_path == source_path))
    if existing and existing.content_hash == content_hash:
        return IngestResult(source_path, len(existing.chunks), False)
    if existing:
        session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == existing.id))
    document = KnowledgeDocument(
        id=str(uuid4()), source_path=source_path, source_type="generated", content_hash=content_hash
    )
    session.add(document)
    session.add(
        KnowledgeChunk(
            id=str(uuid4()),
            document_id=document.id,
            ordinal=0,
            content=content,
            embedding=embedder.embed(content),
            metadata_json=json.dumps({"source": source_path, "ordinal": 0}),
        )
    )
    session.flush()
    return IngestResult(source_path, 1, True)


def _render_live_placeholders(text: str, policy: PolicyConfig) -> str:
    threshold_rupees = policy.auto_approve_below_paise // 100
    threshold = f"Rs {threshold_rupees:,}"
    return (
        text.replace("{{RETURN_WINDOW_DAYS}}", str(policy.return_window_days))
        .replace("{{AUTO_APPROVE_THRESHOLD_INR}}", threshold)
        .replace("{{MAX_REFUNDS_30D}}", str(policy.max_refunds_per_customer_30d))
    )


def _strip_loader_notes(text: str) -> str:
    while "<!--" in text and "-->" in text:
        start = text.index("<!--")
        end = text.index("-->", start) + 3
        text = text[:start] + text[end:]
    return text.strip()


def _read(path: Path, policy: PolicyConfig | None = None) -> tuple[str, str]:
    suffix = path.suffix.casefold()
    source_type = SUPPORTED.get(suffix)
    if source_type is None:
        raise ValueError(f"unsupported knowledge file type: {path.suffix}")
    if source_type == "pdf":
        return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages), source_type
    if source_type == "yaml":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return yaml.safe_dump(raw, sort_keys=True), source_type
    content = path.read_text(encoding="utf-8")
    return _strip_loader_notes(_render_live_placeholders(content, policy) if policy else content), source_type


def _chunks(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    if size <= overlap:
        raise ValueError("chunk size must be greater than overlap")
    clean = " ".join(text.split())
    return [
        clean[start : start + size]
        for start in range(0, len(clean), size - overlap)
        if clean[start : start + size]
    ]


def ingest_path(
    session: Session,
    path: Path,
    embedder: EmbeddingProvider,
    *,
    source_root: Path | None = None,
    policy: PolicyConfig | None = None,
) -> IngestResult:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    text, source_type = _read(path, policy)
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    source_path = str(path.relative_to(source_root.resolve())) if source_root else str(path)
    existing = session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.source_path == source_path))
    if existing and existing.content_hash == content_hash:
        return IngestResult(source_path, len(existing.chunks), False)
    if existing:
        session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == existing.id))
    document = KnowledgeDocument(
        id=str(uuid4()), source_path=source_path, source_type=source_type, content_hash=content_hash
    )
    session.add(document)
    for ordinal, chunk in enumerate(_chunks(text)):
        session.add(
            KnowledgeChunk(
                id=str(uuid4()),
                document_id=document.id,
                ordinal=ordinal,
                content=chunk,
                embedding=embedder.embed(chunk),
                metadata_json=json.dumps({"source": source_path, "ordinal": ordinal}),
            )
        )
    session.flush()
    return IngestResult(source_path, len(_chunks(text)), True)


def ingest_directory(
    session: Session,
    directory: Path,
    embedder: EmbeddingProvider,
    *,
    source_root: Path | None = None,
    policy: PolicyConfig | None = None,
) -> list[IngestResult]:
    directory = directory.resolve()
    if not directory.is_dir():
        raise NotADirectoryError(directory)
    return [
        ingest_path(session, path, embedder, source_root=source_root or directory, policy=policy)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.suffix.casefold() in SUPPORTED
    ]


def ingest_knowledge(
    session: Session,
    *,
    policy_path: Path,
    documents_path: Path | None,
    embedder: EmbeddingProvider,
) -> list[IngestResult]:
    """Ingest the policy source and all supported local knowledge documents."""
    policy = PolicyConfig.model_validate(yaml.safe_load(policy_path.read_text(encoding="utf-8")))
    results = [ingest_path(session, policy_path, embedder, source_root=policy_path.parent, policy=policy)]
    if documents_path is not None and documents_path.exists():
        results.extend(ingest_directory(session, documents_path, embedder, policy=policy))
    session.commit()
    return results
